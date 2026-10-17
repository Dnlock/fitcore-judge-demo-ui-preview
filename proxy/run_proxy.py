#!/usr/bin/env python3
"""ws-judge-core · WS-E 受控代理启动器（公网评委 Demo 一体容器）。

复用 WS-E 已独立复验的 real_proxy.py（只读复制，D2-01 路径安全 + 白名单 +
App 密钥 + 服务端代持 Token + 静态伺服）；本启动器只做两处容器化装配：
1. 监听 0.0.0.0:{PORT}（对外 80；原文件硬编码 127.0.0.1，不修改上游复制件，
   以 monkey-patch server_bind 装配，patch 仅作用于本进程内存）；
2. 收紧 API 白名单：剔除 /docs、/openapi.json（公网演示不需要文档端点，
   减少信息面）。其余白名单、路径安全、响应头剥离逻辑与原文件完全一致。

安全边界（与上游 real_proxy.py 一致）：
- 浏览器零 Token；服务端唯一测试 Token 从环境变量 REAL_API_TOKEN 读取，
  由代理代持注入 Authorization 头；Token 不回显、不落盘、不进日志（无访问日志）；
- 上游只指向容器内 127.0.0.1:8001 隔离 Core（REAL_API_BASE）；
- 静态文件只伺服 web/ 子树（路径穿越拒绝）。

用法:
  REAL_API_TOKEN=... REAL_API_BASE=http://127.0.0.1:8001 python3 proxy/run_proxy.py \
      --port 8956
"""

import argparse
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent          # ws-judge-core/proxy
ROOT = HERE.parent                              # ws-judge-core
sys.path.insert(0, str(HERE))

import real_proxy  # noqa: E402  （WS-E 只读复制件，不修改）

DEFAULT_PORT = 80
DEFAULT_UPSTREAM = "http://127.0.0.1:8001"
DEFAULT_DATA = "/data/realproxy"
# 与上游 4797485 HTTPBoundary 一致的整包上限（WAV 7MB + multipart 余量 64KB）
MAX_MULTIPART_BYTES = 7_000_000 + 65_536

# G6 公网门禁（WS-C 独立验收发现的唯一缺口）：每 Session 上传频率限制。
# 内存滑动窗口，单容器一体部署无跨实例一致性问题；进程重启清零可接受——
# 这是防公网滥用面（429 rate_limited），不是审计记录。
UPLOAD_RATE_WINDOW_SECONDS = 60.0
UPLOAD_RATE_MAX_PER_SESSION = 10
_UPLOAD_RATE_STATE = {}          # sid -> list[monotonic_ts]
_UPLOAD_RATE_LOCK = threading.Lock()
_AUDIO_JOBS_RE = re.compile(r"^/v1/sessions/([^/]{1,120})/audio-jobs$")


def patch_proxy_for_container():
    """容器化装配（仅进程内存，不改复制件）：
    - 静态根目录指向 ws-judge-core/web；
    - 对外绑定 0.0.0.0；
    - 白名单剔除 /docs 与 /openapi.json（公网演示收紧信息面）；
    - 代理层 Content-Length 门禁：整包 > 7MB+64KB 直接 413，不读 body、
      不转发上游（与上游 413 AUDIO_UPLOAD_TOO_LARGE 语义一致；上游对
      chunked/无 Content-Length 请求仍兜底）。
    """
    real_proxy.ROOT = ROOT
    real_proxy.DEFAULT_UPSTREAM = DEFAULT_UPSTREAM
    real_proxy.DEFAULT_DATA_DIR = Path(DEFAULT_DATA)
    real_proxy.RealProxy.ALLOWED_API_PATHS = {
        method: tuple(p for p in paths
                      if p not in ("/docs", "/openapi.json"))
        for method, paths in real_proxy.RealProxy.ALLOWED_API_PATHS.items()
    }

    orig_forward = real_proxy.RealProxy._forward

    def gated_forward(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_MULTIPART_BYTES:
            # 缓冲带内（≤ MAX+8MB）：读掉 body 再回 413，保证客户端可靠收到 413
            #（上游 uvicorn 对超限请求直接重置连接，本代理补齐确定性 413 语义）；
            # 超出缓冲带（异常声明）：不回读，直接应答并断开（拒绝，防 DoS 读盘）。
            if n <= MAX_MULTIPART_BYTES + 8_388_608:
                remaining, chunk = n, 1 << 16
                while remaining > 0:
                    got = self.rfile.read(min(chunk, remaining))
                    if not got:
                        break
                    remaining -= len(got)
                self.close_connection = True  # 早响应连接不复用
            else:
                self.close_connection = True
            return self._err(413, "audio_upload_too_large",
                             f"上传超过限制（整包 ≤ {MAX_MULTIPART_BYTES} 字节，"
                             f"WAV ≤ 7MB；与 Core 413 AUDIO_UPLOAD_TOO_LARGE 语义一致）")
        # G6：每 Session 上传频率限制（仅 POST /v1/sessions/{sid}/audio-jobs）。
        # 超限 → 429 rate_limited，读尽小 body 后应答；不转发上游。
        if method == "POST":
            m = _AUDIO_JOBS_RE.match(
                urllib.parse.urlparse(self.path).path.split("/realproxy", 1)[-1])
            if m:
                sid = m.group(1)
                now = time.monotonic()
                with _UPLOAD_RATE_LOCK:
                    hits = [t for t in _UPLOAD_RATE_STATE.get(sid, [])
                            if now - t < UPLOAD_RATE_WINDOW_SECONDS]
                    if len(hits) >= UPLOAD_RATE_MAX_PER_SESSION:
                        _UPLOAD_RATE_STATE[sid] = hits
                        body = self._request_body()
                        self.close_connection = True
                        return self._err(
                            429, "rate_limited",
                            f"该训练会话的上传过于频繁（限 "
                            f"{UPLOAD_RATE_MAX_PER_SESSION} 次/"
                            f"{int(UPLOAD_RATE_WINDOW_SECONDS)} 秒），请稍后再试")
                    hits.append(now)
                    _UPLOAD_RATE_STATE[sid] = hits
        return orig_forward(self, method)

    real_proxy.RealProxy._forward = gated_forward

    # 绑定 0.0.0.0 由构造参数 server_address 直接决定；必须保留基类
    # HTTPServer.server_bind（其先设置 SO_REUSEADDR 再 bind）——否则端口上
    # 残留的 TIME_WAIT 会使重启报 Address already in use。
    return ThreadingHTTPServer


def main():
    ap = argparse.ArgumentParser(description="ws-judge-core 一体代理（WS-E real_proxy 装配）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PROXY_PORT", DEFAULT_PORT)))
    ap.add_argument("--upstream", default=os.environ.get("REAL_API_BASE", DEFAULT_UPSTREAM))
    ap.add_argument("--data", default=os.environ.get("PROXY_DATA_DIR", DEFAULT_DATA))
    args = ap.parse_args()

    token = (os.environ.get("REAL_API_TOKEN") or "").strip()
    if not token:
        print("[JUDGE-PROXY] 缺少 REAL_API_TOKEN（服务端唯一测试 Token，代理代持，"
              "浏览器零持有）", file=sys.stderr)
        sys.exit(2)

    server_cls = patch_proxy_for_container()
    real_proxy.RealProxy.load_env(args.upstream, Path(args.data))
    real_proxy.RealProxy.startup_meta["port"] = args.port
    real_proxy.RealProxy.startup_meta["containerized"] = "ws-judge-core:bind-0.0.0.0,api-allowlist-tightened"
    srv = server_cls(("0.0.0.0", args.port), real_proxy.RealProxy)

    allowed = real_proxy.RealProxy.ALLOWED_API_PATHS
    print(f"[JUDGE-PROXY] http://0.0.0.0:{args.port}  upstream={real_proxy.RealProxy.upstream}  "
          f"token=服务端代持  GET白名单={len(allowed['GET'])}条 POST白名单={len(allowed['POST'])}条  "
          f"静态根={real_proxy.ROOT / 'web'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
