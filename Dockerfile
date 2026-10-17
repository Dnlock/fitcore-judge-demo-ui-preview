# ws-judge-core · Fitcore 总决赛 P0 公网评委 Demo —— 单容器一体部署
#
# 拓扑：对外 80（WS-E 受控代理，0.0.0.0）→ 容器内 127.0.0.1:8001（隔离 Core）
# 上游业务契约：fitcore-core-real @ 4797485（只读 COPY，运行时只读 import，零修改）
# 平台要求：基础镜像 public.ecr.aws 系（Launch hackathon 集群 Git/Dockerfile 构建）
#
# 构建（示例）:
#   docker build -t fitcore-ws-judge-core:demo .
# 运行（示例；Token 只从环境注入，绝不写进镜像/代码/前端）:
#   docker run -p 80:80 \
#     -e REAL_API_TOKEN="<服务端唯一测试Token>" \
#     -e FITCORE_HTTP_API_TOKEN="<服务端唯一测试Token>" \
#     --tmpfs /data:rw,size=64m \
#     fitcore-ws-judge-core:demo
# 挂载与只读说明见 JUDGE_CORE_CONFIG.md（/app 只读、/data 独立库与锁目录）。

FROM public.ecr.aws/docker/library/python:3.12-slim

LABEL fitcore.component="ws-judge-core" \
      fitcore.upstream.contract="fitcore-core-real@4797485-readonly" \
      fitcore.demo.condition="FAKE_TEST 测试ASR模式（确定性，非真实模型）"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1) 依赖（锁版本；只依赖上游 Core 官方依赖集合，无 ASR 网络依赖）
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir \
        -r /app/requirements.txt \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com

# 2) 业务资产：
#    - proxy/real_proxy.py = WS-E 已独立复验受控代理（只读复制件，D2-01 路径安全）
#    - web/ = 评委界面静态文件（五视图，页面不含 Token；index.html 为 live 注入版）
#    - fixtures/wav/ = FAKE_TEST 演示用合成正弦音 WAV（非真人录音）
COPY proxy/real_proxy.py proxy/run_proxy.py /app/proxy/
COPY web/ /app/web/
COPY fixtures/ /app/fixtures/

# 3) 隔离 Core 启动器与测试 ASR Provider（上游只读 import 包装，不改上游 git 文件）
COPY core/judge_core.py core/fake_test_asr.py /app/core/

# 4) 上游业务契约（只读快照；容器内不执行构建/安装/修改）
COPY fitcore-core-real/app /app/fitcore-core-real/app

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 5) 非 root 运行 + 数据目录（只读挂载说明见 JUDGE_CORE_CONFIG.md）
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin fitcore \
    && mkdir -p /data/realproxy /data/fitcore.db.http-worker.audio-locks \
    && chown -R fitcore:fitcore /data /app
USER fitcore

# 6) 环境变量模板（全部可被 docker run -e 覆盖；Token 必填，缺失拒绝启动）
ENV DATABASE_URL="sqlite:////data/fitcore.db" \
    MODEL_PROVIDER="fake" \
    AUDIO_WORKER_ENABLED="true" \
    AUDIO_TEST_SOURCE_TRUSTED="true" \
    AUDIO_WORKER_POLL_SECONDS="0.05" \
    AUDIO_WORKER_RETRY_SECONDS="0.05" \
    AUDIO_WORKER_MAX_ATTEMPTS="2" \
    CORE_PORT="8001" \
    PROXY_PORT="80" \
    REAL_API_BASE="http://127.0.0.1:8001" \
    CORE_DIR="/app/fitcore-core-real"

# /health 就绪探测（由对外代理代答；代理再转发到 Core /health）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:80/health',timeout=3)"

EXPOSE 80

ENTRYPOINT ["/app/entrypoint.sh"]
