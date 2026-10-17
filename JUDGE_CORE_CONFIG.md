# JUDGE_CORE_CONFIG — 隔离 Demo 后端配置与 12 项公网门禁落实表

- 交付代理：WS-Core 隔离 Demo 后端配置（Fitcore 总决赛 P0）
- 交付日期：2026-10-17（Asia/Shanghai）
- 上游业务契约：`fitcore-core-real @ 479748526bf731b2dfe9292201394019ed7e088c`（与 8790 同版本、独立实例；**只读 COPY + 运行时只读 import，零修改**）
- 状态边界：本交付**不 push、不部署、不碰 8790、不调用付费模型**；本机冒烟只使用端口 8956/8957。

---

## 一、架构

```
评委浏览器（公网，无 Token，仅内置 App 密钥头 X-Demo-App-Key）
        │  http://<公网>:80  （同源，全部经 /realproxy 前缀）
        ▼
┌── WS-E 受控代理（real_proxy.py 只读复制 + run_proxy.py 容器装配）
│   监听 0.0.0.0:80；服务端唯一测试 Token 由环境变量 REAL_API_TOKEN 注入，
│   代理代持注入 Authorization: Bearer；浏览器零持有。
│   白名单：GET 8 条 / POST 7 条（4797485 14 路由剔除 /docs、/openapi.json）。
└───────▶ 隔离 Core：127.0.0.1:8001（仅容器内 loopback，公网不可直达）
           fitcore-core-real@4797485 app 只读 import
           + FAKE_TEST 测试 ASR（sha256→固定转录+固定语义，测试ASR模式）
           + MODEL_PROVIDER=fake（上游内置确定性语义）
           + 独立 SQLite：/data/fitcore.db（不连 8790 库、不连 M1 生产库）
```

- 单容器一体部署（Dockerfile）；AUTH-01 多主体生产接线**不启用**，保持 4797485 单 Token 语义 + 完全隔离演示架构。
- 访客隔离模型：每访客独立 Session（服务端 UUID 发放，无列表/枚举端点）+ 服务端唯一测试 Token（浏览器不可见）+ 前端不能指定他人 Session id。

## 二、12 项公网门禁落实表（逐项：如何满足）

| # | 门禁 | 如何满足 | 实测/验证方式 |
|---|---|---|---|
| 1 | Token 不暴露 | ① 唯一测试 Token 只从环境变量 `REAL_API_TOKEN`/`FITCORE_HTTP_API_TOKEN` 注入（env.example）；② 浏览器源码/静态资产零 Token 字面量（G3 探针实测）；③ 代理不回显 Token、无访问日志、剥离凭据类响应头（STRIP_RESPONSE_HEADERS）；④ Core `access_log=False`。 | E2E `auth_required_401`（直连 Core 无 Bearer→401）；gate P04 `no_token_echo` |
| 2 | 访客隔离 | 每访客独立 Session（服务端 UUID 201 发放）；无会话列表/枚举端点；前端无法获得他人 sid；同服务双访客 facts/pending 互不串扰。 | isolation_smoke G1/G2/G4/G5 |
| 3 | 白名单路径 | 代理 `ALLOWED_API_PATHS` 与 4797485 路由逐条一致（GET 8/POST 7，额外剔除 /docs、/openapi.json）；方法白名单 GET/POST（PUT/DELETE/PATCH 405）；路径单次解码+危险段拒绝（D2-01 已独立复验的 real_proxy 原样保留）。 | gate P02 a–e；WS-C D2-01 复验结论引用 |
| 4 | 上传大小限制 | 上游 4797485 内置：multipart 整包 ≤ `MAX_AUDIO_WAV_BYTES(7MB)+64KB`（HTTPBoundary 预读整包计数，413 `AUDIO_UPLOAD_TOO_LARGE`）；WAV 单文件 ≤ 7MB（413 `AUDIO_TOO_LARGE`）。无需额外配置。 | gate P06 `upload_size_413`（≈7.7MB 包实测 413） |
| 5 | 独立库 | `DATABASE_URL=sqlite:////data/fitcore.db`（容器内）；不连 8790 库、不连 M1 生产库；冒烟用独立 `--db`（默认 `/data`，本机冒烟 `/tmp/ws-judge-core-smoke/fitcore.db`）；上游 fitcore.db 文件只读不动。 | gate P10 upstream≠8790；启动日志 db 路径证据 |
| 6 | 无管理入口 | 无 /admin、无管理路由（代理 404/400）；/docs、/openapi.json 不对外转发；容器内无 shell 服务、无 SSH；非 root 运行。 | gate P02a/b/c |
| 7 | 日志脱敏 | 代理 `log_message` 覆写为空（无访问日志）；Core `access_log=False` + `log_level=warning`；上游 ASR 错误诊断已有密钥/音频正则脱敏（上游代码，未改）；Token 不回显（P04）。 | gate P04；代码审查标注 |
| 8 | 浏览器零持有凭据 | Token 只存在于代理进程内存与 Core 环境；浏览器仅携带 `X-Demo-App-Key`（应用身份，非后端凭据）；401/403 分支由服务端判定。 | E2E `app_key_required_403`、`auth_required_401`；G3 零字面量 |
| 9 | 测试 Provider 确定性 | FAKE_TEST：sha256→固定转录→固定语义（FakeSemanticProvider fixtures）；未映射音频抛 `FAKE_TEST_UNMAPPED_AUDIO`（绝不猜测）；语义 provider=fake 无网络。 | gate P09/P09b；E2E `job_done_8state` final_text 断言 |
| 10 | 不调用付费模型 | `MODEL_PROVIDER=fake` 强制（judge_core.py 非 fake 拒启）；`MODEL_API_KEY` 不注入；FAKE_TEST 零网络（diagnostics.transport=deterministic_fake_test_fixture）；`NUMERIC_RECHECK_ENABLED` 默认关。 | 启动参数证据；ASR diagnostics |
| 11 | 隔离 Core 仅 loopback | Core 绑定 `127.0.0.1:8001`；对外仅 80（代理）；Core 带 Token 校验（无 Bearer 401，E2E 实测）。 | E2E `auth_required_401`；gate P01 |
| 12 | 上传来源服务端声明 | `audio_source` 由服务端前缀 `http_authorized_test:`（测试信任条件 `AUDIO_TEST_SOURCE_TRUSTED=true`）；上传返回 `source_assurance=server_authorized_test`，界面明示“测试ASR模式”；客户端额外字段一律 422。 | E2E `upload_source_assurance`、`extra_form_field_422` |

> 声明：第 12 项的服务端测试信任开关仅用于受控演示/集成测试（上游 4797485 语义）；正式环境必须关闭 `AUDIO_TEST_SOURCE_TRUSTED` 并使用真实 Provider。本配置在界面、Provider 诊断、文档三处标注“测试ASR模式”。

## 三、环境变量模板（env.example）

| 变量 | 默认 | 说明 |
|---|---|---|
| `REAL_API_TOKEN` | 必填 | 服务端唯一测试 Token（代理代持；缺失代理拒绝启动） |
| `FITCORE_HTTP_API_TOKEN` | 必填 | Core 校验用同一测试 Token（缺失 Core 拒绝启动） |
| `DATABASE_URL` | `sqlite:////data/fitcore.db` | 独立 SQLite（容器内） |
| `MODEL_PROVIDER` | `fake`（强制） | 非 fake 拒启（测试语义） |
| `CORE_PORT` | `8001` | Core 监听（127.0.0.1） |
| `PROXY_PORT` | `80` | 对外代理端口（0.0.0.0） |
| `REAL_API_BASE` | `http://127.0.0.1:8001` | 代理上游 |
| `AUDIO_WORKER_ENABLED` | `true` | 上传→ASR→语义单进程流水线 |
| `AUDIO_TEST_SOURCE_TRUSTED` | `true` | 服务端测试信任（仅演示；正式关） |
| `AUDIO_WORKER_POLL/RETRY_SECONDS` | `0.05/0.05` | Worker 轮询/重试（快演示） |
| `AUDIO_WORKER_MAX_ATTEMPTS` | `2` | 上游约束 1..5 |

## 四、Dockerfile 要点与只读挂载说明

- 基础镜像：`public.ecr.aws/docker/library/python:3.12-slim`（平台要求 public.ecr.aws 系）。
- 非 root：`USER fitcore`（uid 10001，nologin）。
- 只读挂载说明（生产化建议）：
  - `/app`：镜像层只读即可（代码+静态文件+上游 app 快照）；建议 `--read-only --tmpfs /tmp` 运行。
  - `/data`：唯一可写目录（fitcore.db + audio-locks + realproxy 元数据）；建议 `--tmpfs /data:rw,size=64m` 或独立 volume（重启即清零=无跨访客残留，天然满足访客隔离）。
- 上传限制无需环境变量（上游内置 7MB/7MB+64KB，见门禁 #4）。
- 健康检查：`/health`（代理代答 `{status,mode,token_configured,upstream,time}`；Core 原生 `/health` 返回 `{status, model_provider}`，经代理可达）。

## 五、本机冒烟证据（8956 代理 → 8957 Core）

- 28 步 E2E：`e2e/smoke_e2e.py` → `E2E_RESULT: ALL_OK 28 steps`（证据：evidence/smoke_e2e.log + evidence/smoke_e2e.exit）
- 门禁探针：`e2e/gate_probe.py` → `GATE_PROBE_RESULT: ALL_PASS 12 probes`
- 访客隔离：`e2e/isolation_smoke.py` → `ISOLATION_RESULT: ALL_OK 8 checks`
- 启动命令（证据目录含完整命令行与时间戳）：
  ```bash
  # Core（127.0.0.1:8957，独立库 /tmp/ws-judge-core-smoke/fitcore.db）
  FITCORE_HTTP_API_TOKEN=<测试Token> python3 core/judge_core.py --port 8957 \
      --db /tmp/ws-judge-core-smoke/final-fitcore.db
  # 代理（0.0.0.0:8956 → 127.0.0.1:8957）
  REAL_API_TOKEN=<同一测试Token> REAL_API_BASE=http://127.0.0.1:8957 \
      python3 proxy/run_proxy.py --port 8956
  ```
- 冒烟 Token 为测试替身值（`ws-judge-core-smoke-test-token`），未落盘真实凭据；冒烟后进程已清理。

## 六、目录清单

| 路径 | 说明 |
|---|---|
| Dockerfile | 单容器编排（80 对外/8001 Core/非 root/只读挂载说明） |
| entrypoint.sh | 一体启动（Core 就绪等待→代理 exec） |
| env.example | 环境变量模板 |
| requirements.txt | 依赖锁定（与上游 pyproject 一致，实测 venv 版本冻结） |
| core/judge_core.py | 隔离 Core 启动器（只读 import + Settings 注入） |
| core/fake_test_asr.py | FAKE_TEST 确定性测试 ASR（sha256→固定转录+固定语义） |
| proxy/real_proxy.py | WS-E 已独立复验代理（只读复制，D2-01） |
| proxy/run_proxy.py | 容器装配（0.0.0.0 绑定 + 白名单收紧，内存级不修改复制件） |
| web/ | 评委界面静态文件（index.html=live 注入版；JS 端口文案已改容器语义） |
| fixtures/wav/ | FAKE_TEST 演示 WAV（合成正弦音，非真人录音） |
| fitcore-core-real/app/ | 上游 4797485 app 只读快照（构建进镜像） |
| e2e/ | smoke_e2e.py（28 步）、gate_probe.py（12 探针）、isolation_smoke.py（8 项） |
| evidence/ | 冒烟证据（日志/退出码/健康检查/库文件） |
