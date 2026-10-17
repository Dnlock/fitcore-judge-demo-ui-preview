#!/usr/bin/env python3
"""ws-judge-core · 隔离 Core 启动器（公网评委 Demo 专用，测试条件）。

原则（与任务书一致）：
- fitcore-core-real（git 4797485 业务契约）以**只读 import** 方式包装：不复制、
  不修改上游任何文件；只通过上游官方注入点 create_app(settings, provider=None,
  asr_provider=...) 注入测试 ASR；
- 独立 SQLite：默认 /data/fitcore.db（容器内），与 8790 库、M1 生产库完全隔离；
- 测试 Provider：MODEL_PROVIDER=fake（上游内置确定性语义）+ FAKE_TEST 确定性 ASR
  （sha256→固定转录+固定语义，测试ASR模式）；零网络、零付费模型调用；
- 服务端持有唯一测试 Token：只从环境变量 FITCORE_HTTP_API_TOKEN 读取
  （不回显、不落盘）；AUTH-01 多主体接线不启用（保持 4797485 单 Token 语义）；
- 监听 127.0.0.1:8001（容器内部 loopback，只有代理可达）；
- /health 返回 {status, model_provider}（上游原生）。

环境变量（模板见 ../../env.example）：
  FITCORE_HTTP_API_TOKEN   服务端侧唯一测试 Token（必填，缺失拒绝启动）
  DATABASE_URL             默认 sqlite:////data/fitcore.db
  MODEL_PROVIDER           固定 fake（测试语义 Provider）
  AUDIO_WORKER_ENABLED     默认 true（上传→ASR→语义 自动流水线）
  AUDIO_TEST_SOURCE_TRUSTED 默认 true（服务端授权测试来源，界面显式标注"测试ASR模式"）
  AUDIO_WORKER_POLL_SECONDS / AUDIO_WORKER_RETRY_SECONDS / AUDIO_WORKER_MAX_ATTEMPTS

用法（本机冒烟）:
  CORE_DIR=<fitcore-core-real> FITCORE_HTTP_API_TOKEN=... python3 core/judge_core.py \
      --port 8957 --db /tmp/ws-judge-core-smoke/fitcore.db
"""

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # ws-judge-core/core
ROOT = HERE.parent                              # ws-judge-core
DEFAULT_CORE_DIR = "/home/dsh-app/workspace/fitcore-core-real"
DEFAULT_PORT = 8001
DEFAULT_DB = "/data/fitcore.db"


def _env_bool(name, default):
    value = os.environ.get(name, "").strip().lower()
    return default if value == "" else value == "true"


def main():
    ap = argparse.ArgumentParser(description="ws-judge-core 隔离 Core（FAKE_TEST 测试条件）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CORE_PORT", DEFAULT_PORT)))
    ap.add_argument("--host", default="127.0.0.1")          # 容器内 loopback；公网不可直达
    ap.add_argument("--core", default=os.environ.get("CORE_DIR", DEFAULT_CORE_DIR))
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_DB}"))
    args = ap.parse_args()

    token = (os.environ.get("FITCORE_HTTP_API_TOKEN") or "").strip()
    if not token:
        print("[JUDGE-CORE] 缺少 FITCORE_HTTP_API_TOKEN（服务端唯一测试 Token，仅环境变量注入）",
              file=sys.stderr)
        sys.exit(2)

    model_provider = os.environ.get("MODEL_PROVIDER", "fake").strip()
    if model_provider != "fake":
        print("[JUDGE-CORE] 公网评委 Demo 只允许 MODEL_PROVIDER=fake（测试语义 Provider，"
              "禁止付费模型）", file=sys.stderr)
        sys.exit(2)

    core_dir = Path(args.core).resolve()
    if not (core_dir / "app" / "main.py").is_file():
        print(f"[JUDGE-CORE] 上游 Core 目录不可用: {core_dir}", file=sys.stderr)
        sys.exit(2)

    db_url = args.db
    if not db_url.startswith("sqlite:///"):
        # 允许直接传文件路径（本机冒烟），自动补 sqlite:/// 前缀
        db_url = "sqlite:///" + (db_url if db_url.startswith("/") else str(Path(db_url).resolve()))
    if not db_url.startswith("sqlite:///"):
        print("[JUDGE-CORE] DATABASE_URL 必须为 sqlite:///（本服务只支持独立 SQLite）",
              file=sys.stderr)
        sys.exit(2)
    Path(db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

    # 关键：上游 app/main.py 在模块导入时执行模块级 create_app()（第 205 行），
    # 其 Settings.from_env() 读 DATABASE_URL（默认 ./fitcore.db，落在 cwd）。
    # 容器内 /app 只读时该默认路径会建库失败/污染上游目录 —— 必须在 import 前
    # 把 DATABASE_URL 指向本服务独立库（绝对路径，与 cwd 无关）。
    os.environ["DATABASE_URL"] = db_url
    os.environ["MODEL_PROVIDER"] = "fake"

    # 只读 import 上游（不修改 fitcore-core-real 任何文件）
    sys.path.insert(0, str(core_dir))
    sys.path.insert(0, str(HERE))
    os.chdir(core_dir)  # 上游相对导入需要以 Core 目录为工作目录

    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        database_url=db_url,
        model_provider="fake",                 # 上游内置确定性语义 fixtures（测试条件）
        model_base_url="",
        model_id="",
        model_api_key="",                      # 零付费模型凭据
        http_api_token=token,                  # 服务端唯一测试 Token（单 Token 语义）
        audio_worker_enabled=True,             # 上传→ASR→语义 单进程流水线
        audio_test_source_trusted=_env_bool("AUDIO_TEST_SOURCE_TRUSTED", True),
        audio_worker_poll_seconds=float(os.environ.get("AUDIO_WORKER_POLL_SECONDS", "0.05")),
        audio_worker_retry_seconds=float(os.environ.get("AUDIO_WORKER_RETRY_SECONDS", "0.05")),
        audio_worker_max_attempts=int(os.environ.get("AUDIO_WORKER_MAX_ATTEMPTS", "2")),
        numeric_recheck_enabled=False,         # 二次 ASR 证据默认关闭（零网络）
    )

    from fake_test_asr import FakeTestASRProvider
    app = create_app(settings, provider=None, asr_provider=FakeTestASRProvider())

    print(f"[JUDGE-CORE] 隔离 Core 启动: http://{args.host}:{args.port}  "
          f"db={db_url}  upstream_core={core_dir}  "
          f"model_provider={settings.model_provider}  asr_provider=FAKE_TEST（测试ASR模式）"
          f"  audio_test_source_trusted={settings.audio_test_source_trusted}", flush=True)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                access_log=False)  # 关闭访问日志：请求路径/参数不进日志（公网脱敏）


if __name__ == "__main__":
    main()
