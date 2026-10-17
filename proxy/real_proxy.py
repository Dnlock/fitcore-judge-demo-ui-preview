#!/usr/bin/env python3
"""Fitcore Real 模式受控代理（WS-E · Phase 4：真实 4797485 联调版）。

职责：浏览器与真实后端之间的同源受控中间层，唯一持有真实后端凭据的一方。
- 浏览器只调本代理（默认 http://127.0.0.1:8814），请求只携带会话 Cookie
  （可选）与内置 App 密钥头 X-Demo-App-Key；浏览器源码与前端静态资产中
  绝无 Bearer Token。
- 真实后端 Token 只从环境变量 REAL_API_TOKEN 读取；缺失时对本代理任何
  /realproxy 请求返回 503（错误体不含任何 Token 值）；Token 不写盘、
  不回显、不进入访问日志（本代理不记录访问日志）。
- 上游 REAL_API_BASE 可配置（Phase 4 默认 http://127.0.0.1:8790，
  真实 Core 4797485 本地运行端口）。
- 转发保持同路径同方法（代理前缀 /realproxy 剥离）；只读请求体 Content-Type；
  不解析、不缓存请求/响应内容（Body 原样透传），因此不落盘任何业务数据。
  对 multipart/form-data 请求不设置 Content-Length（httplib 自动 chunked 传输），
  避免与浏览器边界头冲突。
- 错误路径：Token 缺失→503 missing_token；上游不可达/连接失败→502 upstream_unreachable；
  上游自身错误→原状态码透传（不重写）；非法路径/方法→404/405（按已实现方法计算）。
- 静态页面（Phase 4 新增）：同源伺服 web/ 目录（优先 web/index.live.html，已注入
  演示剧本与 WAV），使 Real 模式页面通过 http://127.0.0.1:8814/ 直接打开；
  静态页不含 Token（Token 只存在于本进程内存）。静态范围仅项目内 web/ 目录，
  不允许路径穿越。
- 权限边界：只写 ws-demo 自己的 .persist/realproxy/ 目录（PID/启动元数据）；
  不写 Token；不允许路径穿越（上游 path 经 urllib 规范化处理）。

运行:
  REAL_API_TOKEN=... python3 server/real_proxy.py --port 8814 \
      [--upstream http://127.0.0.1:8790] [--data .persist/realproxy]
"""

import argparse
import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8814
DEFAULT_UPSTREAM = "http://127.0.0.1:8790"
DEFAULT_DATA_DIR = ROOT / ".persist" / "realproxy"
PROXY_PREFIX = "/realproxy"
# 内置 App 密钥：仅识别演示应用身份（防止同源端口被其它页滥用），
# 与后端凭据无关；后端 Token 从不与它混用。
APP_KEY = "fitcore-demo-app-v1"
APP_KEY_HEADER = "X-Demo-App-Key"
DEFAULT_ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
# 不透传的请求头（代理自管，避免 Token/长度被上游污染）
STRIP_REQUEST_HEADERS = {
    "authorization", "content-length", "host", "connection",
    "accept-encoding", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
    "x-real-ip", "transfer-encoding",
}
# 不转发回浏览器的响应头（凭据绝不回显；含上游异常回显 Authorization 的场景）
STRIP_RESPONSE_HEADERS = {
    "set-cookie", "www-authenticate", "proxy-authenticate", "authorization",
}

HEADERS = {
    "Server": "Fitcore-RealProxy/1.1",
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Demo-App-Key",
    "Access-Control-Allow-Methods": ",".join(DEFAULT_ALLOWED_METHODS) + ",OPTIONS",
}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_body(handler, obj, status):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    for k, v in HEADERS.items():
        handler.send_header(k, v)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class RealProxy(BaseHTTPRequestHandler):
    server_version = "FitcoreRealProxy/1.1"
    upstream = DEFAULT_UPSTREAM
    token = None  # 只放内存（启动时从环境变量读取），绝不写盘/回显
    data_dir = DEFAULT_DATA_DIR
    # 启动记录：只写 ws-demo 自己的 .persist/realproxy/（不含 Token）
    startup_meta = {}
    state_lock = threading.Lock()

    # 不记录访问日志：请求/响应内容（含任何头）绝不进日志
    def log_message(self, *a):
        pass

    @classmethod
    def load_env(cls, upstream, data_dir):
        cls.upstream = upstream.rstrip("/") or DEFAULT_UPSTREAM
        cls.data_dir = data_dir
        cls.token = os.environ.get("REAL_API_TOKEN") or None
        if cls.token is not None:
            cls.token = cls.token.strip() or None
        cls.startup_meta = {
            "started_at": now_iso(),
            "port": None,  # 由 main 填充
            "upstream": cls.upstream,
            "mode": "token_configured" if cls.token else "token_missing",
            "note": "Token 只从环境变量 REAL_API_TOKEN 读取，不落盘不回显",
        }
        try:
            cls.data_dir.mkdir(parents=True, exist_ok=True)
            (cls.data_dir / "meta.json").write_text(
                json.dumps(cls.startup_meta, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass  # 元数据写失败不影响代理运行；目录只在 ws-demo 自己的 .persist 下

    # ------------------------------------------------------------ 响应工具
    def _send_json(self, obj, status):
        _json_body(self, obj, status)

    def _err(self, status, code, msg, extra=None):
        body = {"error": {"code": code, "message": msg,
                          "proxied": False, "time": now_iso()}}
        if extra:
            body["error"].update(extra)
        self._send_json(body, status)

    # ------------------------------------------------------------ 静态页面（Phase 4）
    def _static_path(self, p):
        """URL → 项目内 web/ 文件；仅允许 web/ 子树，拒绝穿越。"""
        if p in ("/", ""):
            p = "/index.html"
        if not p.startswith("/"):
            return None
        live = ROOT / "web" / "index.live.html"
        if p == "/index.html" and live.is_file():
            return live
        # 静态资源 URL（/js/... /css/...）都映射到 web/ 子树
        web_root = (ROOT / "web").resolve()
        target = (web_root / p.lstrip("/")).resolve()
        if web_root not in target.parents or not target.is_file():
            return None
        return target

    def _serve_static(self):
        raw = urllib.parse.urlparse(self.path).path
        # E-SEC-01：静态路径同样单次解码后校验（编码穿越/反斜杠一律拒绝）
        decoded = self._decode_path_once(raw)
        if decoded is None or "%" in decoded or "\\" in decoded:
            return self._err(400, "invalid_path", "静态路径含非法编码/字符")
        target = self._static_path(decoded)
        if target is None:
            return self._err(404, "not_found", "静态路径不存在或越界（仅项目内 web/ 目录）")
        body = target.read_bytes()
        ct = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        # 显式 Real/Mock 模式标识：real 代理页面明示 REAL 模式
        self.send_header("X-Fitcore-Mode", "real")
        self.send_header("Content-Type", ct + ("; charset=utf-8" if ct.startswith("text/") or ct == "application/javascript" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ 转发
    # 白名单：代理只允许转发真实 Core 4797485 的 14 条路由（方法+路径前缀）。
    # 与 Core 路由表严格一致（app/main.py + app/audio/http.py），避免代理与 Core 冲突解释。
    ALLOWED_API_METHODS = {"GET", "POST"}
    # 与 Core 实际路由严格一致（逐条核对 app/main.py 与 app/audio/http.py）
    ALLOWED_API_PATHS = {
        "GET": (
            "/health",
            "/openapi.json",
            "/docs",
            "/v1/sessions/{sid}",
            "/v1/sessions/{sid}/audio-jobs/{uid}",
            "/v1/sessions/{sid}/events",
            "/v1/sessions/{sid}/facts",
            "/v1/sessions/{sid}/pending",
            "/v1/sessions/{sid}/finish",
            "/v1/sessions/{sid}/review",
        ),
        "POST": (
            "/v1/sessions",
            "/v1/sessions/{sid}/audio-jobs",
            "/v1/sessions/{sid}/end",
            "/v1/sessions/{sid}/pending/{pid}/resolve",
            "/v1/sessions/{sid}/finish",
            "/v1/sessions/{sid}/utterances",
            "/v1/sessions/{sid}/utterances/{uid}/reparse",
        ),
    }

    # ------------------------------------------------------------ 路径安全（E-SEC-01 / AUTH-01 D-2）
    @staticmethod
    def _decode_path_once(raw_path):
        """URL 路径单次解码；解码失败（非法 % 序列）返回 None。"""
        try:
            return urllib.parse.unquote(raw_path)
        except Exception:
            return None

    @classmethod
    def _path_allowed(cls, method, path):
        """路径白名单校验。核心原则：代理与 Core 对 URL 使用**同一解释**。
        - 结构匹配用**原始（未解码）路径**完整分段（DEFECT-D2-01）：path.split("/")
          保留空段，段间 // 产生的空段与模板字面段逐位不匹配，不再被 {sid} 吞并；
          %2F 等编码字符不作为路径分隔符（starlette 同样不解码 %2F 为分隔符）。
        - 拒绝任何路径段含编码危险字符（%2e 点号 / %5c 反斜杠 / 双重编码 %25）
          或明文 .. / 反斜杠 —— 无论 Core 如何解释都无歧义。
        - 中段编码斜杠（%2F/%2f 解码后成为段内斜杠）拒绝；仅"末段 + 其后无
          明文尾斜杠 + 解码后恰好一个尾部 /"允许透传，与 Core（starlette 对
          尾斜杠 307 重定向到规范路径）保持同一解释，消除尾部编码斜杠过度拒绝。
        - 明文尾斜杠（/v1/sessions/abc/facts/）允许透传：Core 自行 307 重定向，
          与代理保持同一解释。
        """
        if method not in cls.ALLOWED_API_METHODS:
            return False
        if not path or not path.startswith("/"):
            return False
        # 1) 原始路径完整分段（保留空段）：%2F 不是分隔符；// 产生空段
        raw_full = path.split("/")
        # DEFECT-D2-01：段间空段（//）显式拒绝——空段不得被 {sid}/{uid}/{pid} 吞并
        if any(s == "" for s in raw_full[1:-1]):
            return False
        trailing_slash = False
        if len(raw_full) > 1 and raw_full[-1] == "":
            trailing_slash = True
            raw_full = raw_full[:-1]  # 明文尾斜杠：透传，Core 自行 307
        raw_segments = raw_full
        # 2) 每个段拒绝危险内容（原始与单次解码双重检查）
        last = len(raw_segments) - 1
        for i in range(1, len(raw_segments)):
            seg = raw_segments[i]
            if seg in ("..", "."):
                return False
            if "\\" in seg:
                return False
            if "%" in seg:
                dec = cls._decode_path_once(seg)
                if dec is None:
                    return False
                # 解码后不得出现危险字符/残留编码（双重编码）
                if "%" in dec or "\\" in dec or ".." in dec:
                    return False
                if "/" in dec:
                    # 仅"末段、无明文尾斜杠、解码后恰好单个尾部 /"允许（Core 307 一致）；
                    # 中段编码斜杠（含编码斜杠后跟明文斜杠）一律拒绝
                    if not (i == last and not trailing_slash
                            and dec.count("/") == 1 and dec.endswith("/")
                            and len(dec) >= 2):
                        return False
        # 3) 结构匹配：完整分段（保留空段语义）对白名单模板
        match_segs = list(raw_segments)
        if "%" in match_segs[-1]:
            dec = cls._decode_path_once(match_segs[-1])
            if (dec is not None and dec.count("/") == 1 and dec.endswith("/")
                    and len(dec) >= 2):
                # 尾部编码斜杠：仅当末段是静态字面段时按 Core 语义规范化（去掉
                # 尾部 /）后匹配；{sid}/{uid}/{pid} 运行时段不享受此放宽（保守拒绝）
                tpl_last = [t.split("/")[-1] for t in
                            cls.ALLOWED_API_PATHS.get(method, ())]
                if match_segs[-1][:-3] not in tpl_last:
                    return False
                match_segs[-1] = dec[:-1]
        for template in cls.ALLOWED_API_PATHS.get(method, ()):
            tsegs = template.split("/")
            if len(match_segs) != len(tsegs):
                continue
            ok = all(t in ("{sid}", "{uid}", "{pid}") or t == s
                     for s, t in zip(match_segs, tsegs))
            if ok:
                return True
        return False

    def _proxy_path(self, p):
        """/realproxy[/...] -> 上游 /[...]。"""
        if p == PROXY_PREFIX:
            return "/"
        return p[len(PROXY_PREFIX):]

    def _request_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _forward(self, method):
        # 1) Token 门禁：缺失时报 503，错误体不含任何凭据
        if not self.token:
            return self._err(503, "missing_token",
                             "Real 模式代理未配置真实后端 Token：请在服务端环境变量 "
                             "REAL_API_TOKEN 中注入（绝不写入代码/前端/日志），重启代理后重试")
        # 2) 内置 App 密钥（演示应用身份识别；与后端 Token 无关）
        if self.headers.get(APP_KEY_HEADER) != APP_KEY:
            return self._err(403, "app_key_required",
                             "缺少内置 App 密钥头 X-Demo-App-Key（演示应用身份识别，非后端凭据）")
        # 3) 路径安全检查（E-SEC-01：单次解码→危险段→白名单）
        raw_request_path = urllib.parse.urlparse(self.path).path
        if not raw_request_path.startswith(PROXY_PREFIX + "/") and raw_request_path != PROXY_PREFIX:
            return self._err(404, "not_found", "Real 代理只接受 /realproxy 前缀路径")
        upstream_path = self._proxy_path(raw_request_path)
        if not self._path_allowed(method, upstream_path):
            return self._err(400, "invalid_path",
                             "路径不合法（编码/穿越/非白名单 API；代理与 Core 使用同一 URL 解释）")
        # 4) 组装上游 URL：路径原样转发（不预解码/不重编码，与 Core 同一解释）
        url = self.upstream + upstream_path
        if urllib.parse.urlparse(self.path).query:
            url += "?" + urllib.parse.urlparse(self.path).query
        body = self._request_body()
        req = urllib.request.Request(url, data=body if body else None, method=method)
        ct = self.headers.get("Content-Type")
        if ct:
            req.add_header("Content-Type", ct)
        is_multipart = bool(ct and "multipart/form-data" in ct)
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in STRIP_REQUEST_HEADERS or k == APP_KEY_HEADER:
                continue
            # multipart：Content-Length 由 httplib 按 chunked 自动处理（跳过，避免边界冲突）
            if is_multipart and lk == "content-length":
                continue
            req.add_header(k, v)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        # 4) 转发（透明透传；上游错误码原样返回）
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                status = r.status
                headers = list(r.headers.items())
        except urllib.error.HTTPError as e:
            data = e.read()
            status = e.code
            headers = list(e.headers.items())
        except urllib.error.URLError as e:
            return self._err(502, "upstream_unreachable",
                             "真实后端不可达（Real 代理无法连接上游 REAL_API_BASE）",
                             {"upstream": self.upstream, "reason": str(e.reason or e)})
        except Exception as e:  # 网络/超时等未知传输错误同样归为上游不可达
            return self._err(502, "upstream_unreachable",
                             "真实后端不可达（转发异常）",
                             {"upstream": self.upstream, "reason": str(e)[:200]})
        # 5) 回传（凭据类响应头不回显给浏览器）
        self.send_response(status)
        for k, v in headers:
            if k.lower() in STRIP_RESPONSE_HEADERS:
                continue
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        # 显式 Real/Mock 模式标识：经 real 代理的响应明确标注 REAL
        self.send_header("X-Fitcore-Mode", "real")
        if not any(k.lower() == "content-length" for k, _ in headers):
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------ HTTP 方法
    def _unsupported(self):
        self._err(405, "method_not_allowed",
                  "Real 代理仅支持: " + ", ".join(sorted(DEFAULT_ALLOWED_METHODS)))

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/health":
            return self._send_json({
                "status": "ok",
                "mode": "real_proxy",
                "token_configured": bool(self.token),
                "upstream": self.upstream,
                "time": now_iso(),
            }, 200)
        if p.startswith(PROXY_PREFIX):
            return self._forward("GET")
        return self._serve_static()

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path.startswith(PROXY_PREFIX):
            return self._forward("POST")
        return self._err(404, "not_found", "Real 代理路由: /health、/ 静态页或 /realproxy/...")

    def do_PUT(self):
        # E-SEC-01：白名单外方法一律拒绝（真实 Core 无 PUT 路由）
        return self._err(405, "method_not_allowed",
                         "白名单外的 HTTP 方法被拒绝（仅 GET/POST/OPTIONS）")

    def do_DELETE(self):
        return self._err(405, "method_not_allowed",
                         "白名单外的 HTTP 方法被拒绝（仅 GET/POST/OPTIONS）")

    def do_PATCH(self):
        return self._err(405, "method_not_allowed",
                         "白名单外的 HTTP 方法被拒绝（仅 GET/POST/OPTIONS）")

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    ap = argparse.ArgumentParser(description="Fitcore Real 模式受控代理（Phase 4）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--upstream", default=os.environ.get("REAL_API_BASE", DEFAULT_UPSTREAM))
    ap.add_argument("--data", default=str(DEFAULT_DATA_DIR))
    args = ap.parse_args()
    RealProxy.load_env(args.upstream, Path(args.data))
    RealProxy.startup_meta["port"] = args.port
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), RealProxy)
    mode = "token 已配置" if RealProxy.token else "TOKEN 缺失（/realproxy 将返回 503）"
    print(f"[REAL-PROXY] http://127.0.0.1:{args.port}  upstream={RealProxy.upstream}  "
          f"{mode}  静态页: /  数据目录: {args.data}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
