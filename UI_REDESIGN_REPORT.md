# Fitcore 评委网页 UI 重构报告（ui-rebuild）

**范围**：展示层重构（web/index.html + web/css/styles.css + web/js/ui-adapter.js）；业务层零改动。
**设计母版**：`/home/dsh-app/workspace/ui-reference/Fitcore_UI_Reference/mobile_final(2).html` + `README_UI_REBUILD.md`（Token 原值）。

---

## 一、交付物

| 路径 | 说明 |
|---|---|
| `web/index.html` | 五页母版产品结构（手机壳 + Hero + 录音豆 + context-card + live-card + 底部 Bean 导航）；`fitcore-demo-data` 演示剧本 JSON 与已验证版逐字一致 |
| `web/css/styles.css` | 全部替换为母版 Design Token 体系（Navy/主蓝/Lime/Canvas/Ink/Round/shadow 原值） |
| `web/js/app.js` | 与 ws-judge-core 逐字节一致（未改） |
| `web/js/httpAdapter.js`、`web/js/v1client.js` | 与 ws-judge-core 逐字节一致（未改） |
| `web/js/ui-adapter.js` | 新增展示层适配（母版 DOM 映射 + 导航绑定 + 派生展示；不触碰业务逻辑） |
| `proxy/`、`core/`、`fixtures/`、`entrypoint.sh`、`Dockerfile`、`requirements.txt`、`env.example` | 复制自 ws-judge-core（部署组装基座，未改） |
| `e2e/smoke_e2e.py` | 与 ws-judge-core 逐字节一致（28 步，未改断言） |
| `e2e/verify_design_tokens.py` | 新增：母版设计 Token 静态断言脚本 |
| `UI_REDESIGN_REPORT.md` | 本报告 |

## 二、母版对齐逐项对照

### 设计 Token（CSS 变量 = 母版原值，verify_design_tokens.py 逐项断言 44/44 PASS）

| Token | 母版原值 | ui-rebuild 值 | 结果 |
|---|---|---|---|
| Navy | `#112a49` | `#112a49` | ✅ |
| Navy2 | `#17385f` | `#17385f` | ✅ |
| 主蓝 | `#3c72df` | `#3c72df` | ✅ |
| 辅蓝 | `#5a88e8` | `#5a88e8` | ✅ |
| Lime | `#c8ef63` | `#c8ef63` | ✅ |
| Canvas | `#edf2f7` | `#edf2f7` | ✅ |
| Card | `#ffffff` | `#fff` | ✅ |
| Ink | `#17283d` | `#17283d` | ✅ |
| Muted | `#75849a` | `#75849a` | ✅ |
| Border | `#dce4ee` | `#dce4ee` | ✅ |
| shadow | `0 14px 34px rgba(28,54,88,.10)` | 同原值 | ✅ |
| 圆角 | 20–24px | 卡片 20px / Hero 24px / 按钮 13px（同母版） | ✅ |
| 禁止回归 | `#0D1117` 黑底 / `#4DD0A1` 整页绿 | 已从声明层移除（仅存在于注释说明） | ✅ |

### 结构对照（母版 → ui-rebuild）

| 母版结构 | ui-rebuild 实现 |
|---|---|
| demo-stage 居中手机壳（.phone 393px / 47px 圆角 / 深色边框 / 刘海） | ✅ 同结构同值；>430px 居中投屏，≤430px 铺满 |
| 状态栏（9:41 / 信号 / 电池） | ✅ |
| 深蓝 Hero（TODAY'S TRAINING / 杠铃卧推 / 大数字 80kg / 上次训练 / trend-mini） | ✅ 静态展示层同母版；「上次训练」改由真实 Facts 派生（见 §五-3） |
| 白色卡片 + bean-row（74px 圆形录音豆 + LED + 文案） | ✅ 录音豆用 CSS 渐变绘制（navy 圆形 + 白描边 + Lime LED，标注「网页 CSS 绘制占位图」，未杜撰硬件连接） |
| 蓝色主按钮 | ✅ `.primary`（同母版渐变阴影规格） |
| context-card（深蓝 SET 1/3 + 当前动作/重量） | ✅ 训练中页首屏；数据由 Core ContextRead 派生（见 §五-1） |
| live-card（wave 动画 / 大状态 / quote / saved-result metric） | ✅ 全部在；上传轮询中 wave 自动 `live` 动画，状态行变「● PROCESSING」 |
| 训练事件列表 | ✅ `.event-list`（训练中 / 记录 / 待确认 / 复盘共用） |
| 复盘大数字 | ✅ `.summary-grid` 三列（总组数 / 总次数 / 训练容量，来自 ReviewRead） |
| 中央 Bean 底部导航（今日/历史/Bean/AI分析/我的） | ✅ 五格结构 + 中央 navy Bean（CSS 绘制）；标签按五页业务语义命名（今日/记录/Bean=训练中/待确认/复盘），中央按钮即「训练中」 |

### 五页信息层级对照（README_UI_REBUILD 验收 1-3 条）

| 页 | 要求 | 实现 |
|---|---|---|
| 首页 | Hero + 开始训练主 CTA + 录音豆 ready-card + 2-3 行引导；测试ASR模式 chip 常驻；Session ID/HTTP/Job 术语不进首屏 | ✅ `home-mode-badge` 渲染为 chip（「REAL 模式 · 测试ASR模式」）；场景选择/剧本明细/模式说明全部收进 `<details class="demo-info">` |
| 训练中 | context-card + live-card + 最新训练事件卡 | ✅ 首屏即 Context/波形/主 CTA/saved-result/本次记录；上传、预置语音、Job 状态、Session 明细均入折叠区 |
| 记录 | 白底训练组卡（动作/重量/次数大字） | ✅ `facts-list` 卡片（由 GET facts 表格派生渲染）；原始明细表保留在折叠区 |
| 待确认 | pending 卡 + 确认/跳过 + RESOLVED/DUPLICATE/409 细分 + 纠错「暂不可用」 | ✅ 正式 resolve 接口不变；反馈横幅 ok/error/pending 三态；纠错卡常显「暂不可用」说明 |
| 复盘 | 大数字 + 已确认组列表 + 返回首页/重新开始 | ✅ review-gate 门禁保留（Finish 成功才显示）；大数字/列表均来自 ReviewRead |

## 三、展示层如何做到业务零改动

1. **app.js 逐字节未改**：所有 DOM id（start-status/session-card/preset-audio/job-list/set-queue/facts-table/facts-empty/facts-session/pending-list/pending-empty-hint/resolve-result/correction-list/correction-hint/review-stats/review-list/review-meta/review-empty/review-gate/review-body/finish-status/progress/home-audio-list/scene-select/scene-list/start-hint/home-mode-badge/start-actions/upload-progress-*/retry-banner/mode-banner/wav-file/next-btn/end-btn/finish-btn/record-status/pending-summary）在新 HTML 中原样保留，渲染/绑定层行为与已验证版一致。
2. **新 UI 复用其渲染锚点，不新增状态写入**：已写入锚点直接展示（chip 徽标、折叠区明细）；母版新锚点（context-card/live-card/复盘大数字/记录卡片）由 `ui-adapter.js` 纯展示层派生：读 `FitcoreDemoInternals.snapshot()`（app.js 已暴露）与既有 DOM，做样式化映射；**不发起任何 API、不写 state、不改 session/localStorage、不伪造数据**。
3. HTTP Adapter（/realproxy、App-Key、Bearer 零持有）、429 频率门禁、路径白名单、Finish Barrier、409 语义、Session 隔离全部原样保留。

## 四、功能回归（全部实跑）

| 测试 | 结果 |
|---|---|
| e2e/smoke_e2e.py（28 步，隔离 Core 8975 + 代理 8976，/tmp 环境） | **28/28 ALL_OK**（含 static_page_real_mode：代理伺服的新 index.html，`X-Fitcore-Mode: real`） |
| e2e/verify_design_tokens.py（母版 Token 原值 + 结构 + 禁色 + 脚本引用） | **44/44 ALL_OK** |
| jsdom 离线 DOM 冒烟（新页面 + app.js 初始化 + ui-adapter 无 JS 异常 + 页面切换） | **38/38 PASS** |
| jsdom REAL 链路浏览器冒烟（真实 8976 代理：开始训练 201 → 提交预置语音 → facts 60kg×8 卡片渲染 → saved-result → 结束训练 → Review 大数字 → 五页切换） | **16/16 PASS** |
| node --check（app/httpAdapter/v1client/ui-adapter） | 语法全过 |

- 发现并修复 1 个适配层缺陷：ui-adapter 初始版对 facts-list/set-queue 的 innerHTML 重建触发 MutationObserver 自循环（微任务饿死事件循环）；改为「内容相同则跳过写入」保护后消除。**该缺陷仅在新增 ui-adapter.js 中，业务文件未受影响。**
- 未触碰 WS-C judge_gates/judge_e2e（要求不改，未重跑）。

## 五、实现说明（与「禁止伪造」要求对照）

1. **Hero 大数字 80kg / SET 1/3 / Target 8–10**：母版静态叙事数字。真实 Context（GET session）不含这些字段（active_exercise_name / last_reported_weight / next_set_plan 在测试剧本里通常为空），且业务脚本预置会话不自带训练计划；因此 80kg 仅作为母版 Hero 视觉占位保留，训练中页的当前重量/动作/组次一律取真实 ContextRead/Facts（映射见 ui-adapter）。若将来真实 Context 返回 next_set_plan，再接入。
2. **录音豆**：母版 `preview-assets/bean-white-mockup.png` 不在包内且无授权素材；按任务书允许方案用 CSS 渐变绘制占位（navy 圆 + 白描边 + Lime LED + 白色内圆），页面明示「网页 CSS 绘制占位图」，未杜撰「已连接实体硬件」。
3. **上次训练 / 最近同重量首组**：Home Hero 静态展示母版层级；「上次训练」在真实会话产生事实后由 GET facts 末行派生（「60kg × 8 · 第 1 组」），无事实时保持「开始训练后读取真实会话」占位。
4. **演示语音**：预置清单（E-0002…E-0006 合成测试 WAV）从首页折叠区移入训练中「演示语音」折叠区；提交路径不变（submitPreset → 真实 audio-jobs → 202 → 8 态轮询）。

## 六、NOT_RUN 项（无图形环境）

| 项 | 状态 | 说明 |
|---|---|---|
| 五页真实浏览器截图对照母版 | **NOT_RUN** | 本环境无图形界面（无 headless Chrome/Puppeteer 已装）。已用 jsdom 完成 DOM 级冒烟（38+16 断言）。 |
| 视觉回归（Chrome 桌面 + iPhone Safari 截图/录屏对照母版） | **NOT_RUN** | 同上。 |
| JUDGE_UI_REDESIGN_READY=YES | **不宣称** | 按 README 要求：成功部署 + 浏览器实测记录后才可宣称；本任务未部署。 |

### 浏览器截图对照方法（供人工执行）

```bash
# 1) 起本地栈（/tmp 冒烟环境即可，或复用正式容器）
cd ui-rebuild
CORE_DIR=/home/dsh-app/workspace/fitcore-core-real \
FITCORE_HTTP_API_TOKEN=<测试Token> DATABASE_URL=sqlite:////tmp/fitcore.db \
  .venv/bin/python core/judge_core.py --port 8975 --db /tmp/fitcore.db &
REAL_API_TOKEN=<同一Token> REAL_API_BASE=http://127.0.0.1:8975 \
  .venv/bin/python proxy/run_proxy.py --port 8976 &
# 2) 浏览器打开 http://127.0.0.1:8976/
#    - 桌面：居中手机壳（393px）；窗口 <430px：铺满
#    - 首页对照母版：Hero / 录音豆 / 开始训练 / 底部 Bean 导航
#    - 开始训练 → 训练中：Context 卡 / 波形 / 演示语音折叠区提交 E-0002
#    - 记录/待确认/复盘：五页切换 + Finish + 大数字
#    - Chrome DevTools 设备模式选 iPhone（390×844）截图，与 mobile_final(2).html 并排对照
# 3) 截图保存后按 README 验收第 4 条分别给出视觉回归 PASS/FAIL
```

## 七、如何本地预览

```bash
cd ui-rebuild
python3 -m http.server 8977 --bind 127.0.0.1
# 浏览器打开 http://127.0.0.1:8977/
```
- 纯静态预览时（无代理）：页面以 offline 回放模式运行演示剧本（真实接口在代理栈启动后经 `/realproxy` 生效，见 §六步骤）。
- 本环境预览链接：`http://127.0.0.1:8977/`（临时静态服务，交付后按需重启）。

## 八、已知限制（如实说明）

- 视觉截图未实测（NOT_RUN）；DOM/功能已实跑验证。
- 母版 trend-mini 柱与「最近同重量首组」为静态视觉（真实后端无历史会话数据）。
- 底部导航「今日/记录/Bean/待确认/复盘」命名按五页业务语义微调自母版「今日/历史/Bean/AI分析/我的」；结构（中央 Bean + 五格）不变。
- HEAD 请求 501：继承自上游 real_proxy.py 行为，浏览器 GET 正常（未改代理）。
