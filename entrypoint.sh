#!/bin/bash
# ws-judge-core · 单容器一体部署入口：WS-E 代理(80) + 隔离 Core(127.0.0.1:8001)
# 非 root 用户运行（Dockerfile USER fitcore）；只读挂载说明见 JUDGE_CORE_CONFIG.md
set -eu

: "${REAL_API_TOKEN:?必须注入 REAL_API_TOKEN（服务端唯一测试 Token，代理代持）}"
: "${FITCORE_HTTP_API_TOKEN:?必须注入 FITCORE_HTTP_API_TOKEN（服务端唯一测试 Token，Core 校验）}"

export DATABASE_URL="${DATABASE_URL:-sqlite:////data/fitcore.db}"
export MODEL_PROVIDER="fake"                      # 公网演示强制测试语义 Provider
export AUDIO_WORKER_ENABLED="${AUDIO_WORKER_ENABLED:-true}"
export AUDIO_TEST_SOURCE_TRUSTED="${AUDIO_TEST_SOURCE_TRUSTED:-true}"   # 服务端测试信任（界面标注）
export CORE_PORT="${CORE_PORT:-8001}"
export PROXY_PORT="${PROXY_PORT:-80}"
export REAL_API_BASE="${REAL_API_BASE:-http://127.0.0.1:8001}"
export CORE_DIR="${CORE_DIR:-/app/fitcore-core-real}"   # 上游 4797485 app 只读快照（镜像内）

mkdir -p /data/realproxy /data/fitcore.db.http-worker.audio-locks

echo "[JUDGE-ENTRY] 启动隔离 Core 127.0.0.1:${CORE_PORT}（db=${DATABASE_URL}，FAKE_TEST 测试ASR模式）"
python3 /app/core/judge_core.py --port "${CORE_PORT}" \
    >/tmp/judge-core.log 2>&1 &
CORE_PID=$!

# 就绪等待（最多 15s）：Core 只监听 loopback，健康检查由代理代答
python3 - <<'PY' "$CORE_PID" "$CORE_PORT"
import sys, time, urllib.request
pid = int(sys.argv[1])
port = int(sys.argv[2])
deadline = time.time() + 15
while time.time() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
            if r.status == 200:
                sys.exit(0)
    except Exception:
        pass
    import os
    if not os.path.exists(f"/proc/{pid}"):
        print("[JUDGE-ENTRY] Core 进程已退出", file=sys.stderr)
        sys.exit(2)
    time.sleep(0.25)
print("[JUDGE-ENTRY] Core 健康检查超时", file=sys.stderr)
sys.exit(3)
PY

echo "[JUDGE-ENTRY] Core 就绪，启动对外代理 0.0.0.0:${PROXY_PORT}"
exec python3 /app/proxy/run_proxy.py --port "${PROXY_PORT}"
