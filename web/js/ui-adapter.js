/* ============================================================================
 * ui-rebuild · 展示层 DOM 适配补丁（设计母版映射层）
 *
 * 铁律：业务逻辑零改动。本文件不修改 app.js 的任何函数、不重新实现任何
 * 传输/接口/状态机逻辑；只做三件事：
 *   1. 渲染钩子：在 render() 之后，把已验证的 app.js 文本渲染结果映射到
 *      设计母版结构（母版结构 id 与旧调试结构 id 一一映射，见 ID_MAP）；
 *   2. 交互绑定：底部 Bean 导航（母版五页切换）、返回按钮（data-go）、
 *      中央 Bean 按钮（训练中）、错误 toast；
 *   3. 动态样式态：上传/轮询时波形 live 动画与状态行文案、折叠区开合标注。
 *
 * 依赖顺序：必须在 app.js 之后加载（window.FitcoreDemo 已存在时才绑定）。
 * ==========================================================================*/
(function () {
  "use strict";
  if (typeof window.FitcoreDemo === "undefined") return; // app.js 未加载（保护）

  // ---------- 母版结构 id ↔ app.js 渲染锚点 id 映射 ----------
  var ID_MAP = {
    // 首页
    "home-mode-badge": "home-mode-badge",        // 测试ASR模式 chip（常驻、非巨型横幅）
    "home-status-label": "home-status-label",    // Session Ready / Session <id> · <status>
    "start-status": "start-status",              // 尚未创建会话 / 会话状态
    "start-hint": "start-hint",                  // 简短引导（2-3 行）
    "start-actions": "start-actions",            // 开始训练主 CTA
    "home-audio-list": "home-audio-list",        // 预置演示语音清单
    "scene-select": "scene-select",              // 演示剧本选择（折叠区内）
    "scene-list": "scene-list",                  // 剧本事件明细（折叠区内）
    // 训练中
    "context-eyebrow": "context-eyebrow",        // 动作英文标签（ContextRead）
    "context-exercise": "context-exercise",      // 当前动作（ContextRead）
    "currentWeight": "currentWeight",            // 当前重量（ContextRead）
    "setLabel": "setLabel",                      // SET n / 3（Facts 推导）
    "targetLabel": "targetLabel",                // Target 8–10 reps（next_set_plan）
    "activeStatus": "activeStatus",              // 右上 Listening / 状态
    "stateLabel": "stateLabel",                  // ● LISTENING（上传中动态态）
    "stateCopy": "stateCopy",                    // 等待训练语音 / 上传进度
    "wave": "wave",                              // 波形（上传/轮询时 live 动画）
    "liveTitle": "liveTitle",                    // 大状态标题
    "liveBody": "liveBody",                      // 大状态说明
    "quoteBox": "quoteBox",                      // 当前 Context 引用行
    "savedResult": "savedResult",                // saved-result 指标（最新完成组）
    "eventMain": "eventMain",                    // Training Event 80 × 9
    "eventState": "eventState",                  // 状态 RIR2 / 已入账
    "session-card": "session-card",              // 会话详情（折叠区）
    "preset-audio": "preset-audio",              // 预置演示语音（折叠区）
    "job-list": "job-list",                      // 作业状态（折叠区）
    "progress": "progress",                      // 演示进度（折叠区）
    "record-status": "record-status",            // 最新事件状态
    "set-queue": "set-queue",                    // 本次记录列表
    "recordCount": "recordCount",                // 已记录 n / 3 组
    "next-btn": "next-btn",                      // 单个主 CTA
    "end-btn": "end-btn",                        // 结束训练
    "upload-btn": "upload-btn",                  // 上传 WAV
    "wav-file": "wav-file",                      // 文件输入
    "upload-progress": "upload-progress",        // 进度条
    "upload-progress-bar": "upload-progress-bar",
    "upload-progress-text": "upload-progress-text",
    "pending-summary": "pending-summary",        // 待确认概览
    // 训练记录
    "facts-session": "facts-session",            // 会话与数据源（折叠区）
    "facts-empty": "facts-empty",                // 空态提示
    "facts-table": "facts-table",                // 明细表（折叠区）
    "facts-list": "facts-list",                  // 白底训练组卡（主展示）
    // 待确认
    "resolve-result": "resolve-result",          // RESOLVED / DUPLICATE / 409 反馈
    "pending-list": "pending-list",              // pending 卡片
    "pending-empty-hint": "pending-empty-hint",  // 规则提示
    "correction-unavailable": "correction-unavailable", // 纠错「暂不可用」
    // 纠错辅助视图
    "correction-list": "correction-list",
    "correction-hint": "correction-hint",
    // 复盘
    "finish-btn": "finish-btn",
    "finish-status": "finish-status",
    "review-gate": "review-gate",
    "review-gate-label": "review-gate-label",
    "review-empty": "review-empty",
    "review-body": "review-body",
    "review-stats": "review-stats",              // 大数字指标
    "review-list": "review-list",                // 已确认组列表
    "review-meta": "review-meta",
    // 全局
    "mode-banner": "mode-banner",                // 无元素则跳过（模式徽标已在首页 chip）
    "retry-banner": "retry-banner",
    "toast": "toast",
  };

  function $(id) { return document.getElementById(id); }
  function el(id) { return $(ID_MAP[id] || id); }

  // ---------- 母版交互：导航 / 返回 / 中央 Bean / toast ----------
  function bindNav() {
    var nav = document.querySelector(".nav");
    if (!nav) return;
    // 母版 nav 按钮为 data-view；app.js switchView 已负责 page 切换与 active 样式
    nav.querySelectorAll("button[data-view]").forEach(function (b) {
      b.addEventListener("click", function () {
        try {
          window.FitcoreDemo.view(b.dataset.view);
          syncActiveNav(b.dataset.view);
        } catch (e) { /* 无会话等场景由 app.js 自身 render 兜底 */ }
      });
    });
    // 返回按钮（subhead .back）：回到目标页
    document.querySelectorAll(".back[data-go]").forEach(function (b) {
      b.addEventListener("click", function () {
        try { window.FitcoreDemo.view(b.dataset.go); } catch (e) { /* ignore */ }
      });
    });
    // 中央 Bean：训练中页
    var bean = $("navBean");
    if (bean) {
      bean.addEventListener("click", function () {
        try { window.FitcoreDemo.toActive(); } catch (e) { /* ignore */ }
      });
    }
    // 首页开始训练按钮由 app.js 动态渲染（start-actions），无需额外绑定。
    window.FitcoreDemoInternals.showToast = showToast;
  }

  function syncActiveNav(view) {
    document.querySelectorAll(".nav button[data-view]").forEach(function (b) {
      b.classList.toggle("active", b.dataset.view === view);
    });
  }

  var toastTimer = null;
  function showToast(text) {
    var t = $("toast");
    if (!t) return;
    t.textContent = text;
    t.classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("show"); }, 2600);
  }

  // ---------- 渲染钩子：app.js render() 后同步映射母版结构 ----------

  function syncFromAppDom() {
    // 1) 首页徽标与状态文案
    copyText("home-mode-badge");        // 测试ASR模式 chip 常驻
    copyText("home-status-label");
    copyText("start-status");
    copyText("start-hint");
    deriveHeroMemory();                 // Hero 上次训练（Facts 派生，非伪造）
    // 2) 折叠区明细
    copyText("session-card");
    copyText("progress");
    copyText("record-status");
    copyText("pending-summary");
    copyText("facts-session");
    copyText("facts-empty");
    copyText("correction-hint");
    copyText("finish-status");
    copyText("review-meta");
    copyHtml("job-list");
    copyHtml("preset-audio");
    copyHtml("pending-list");
    copyHtml("review-list");
    copyHtml("review-stats");
    copyHtml("review-empty");
    copyHtml("resolve-result");
    // 3) 主展示区映射（卡片化）
    mapFactsList();
    mapSetQueue();
    // 4) 训练中派生展示（Context 卡 / 大状态 / saved-result）
    deriveActiveView();
    // 5) 复盘概览 grid 风格化
    stylizeReviewStats();
    // 6) 纠错卡片标注
    markCorrectionUnavailable();
  }

  // ---------- 写入保护：内容相同跳过（防止 MutationObserver 微任务自循环） ----------
  var lastWrite = {};
  function writeText(id, text) {
    var d = el(id);
    if (!d) return;
    if (lastWrite[id + "#text"] === text) return;
    lastWrite[id + "#text"] = text;
    d.textContent = text;
  }
  function writeHtml(id, html) {
    var d = el(id);
    if (!d) return;
    if (lastWrite[id + "#html"] === html) return;
    lastWrite[id + "#html"] = html;
    d.innerHTML = html;
  }
  function writeClass(id, cls, on) {
    var d = el(id);
    if (!d) return;
    d.classList.toggle(cls, !!on);
  }

  function copyText(id) {
    var d = el(id);
    var s = $(id);
    if (d && s && d !== s) {
      writeText(id, s.textContent);
    }
  }
  function copyHtml(id) {
    var d = el(id);
    var s = $(id);
    if (d && s && d !== s) {
      writeHtml(id, s.innerHTML);
      d.classList.toggle("hidden", s.classList.contains("hidden"));
    }
  }

  // ---------- Hero「上次训练」真实 Context 回填（源自 Facts 已确认组，不伪造） ----------
  function deriveHeroMemory() {
    var dst = $("previousSet");
    var rows = document.querySelectorAll("#facts-table tbody tr");
    if (!dst || !rows.length) return;
    var last = rows[rows.length - 1];
    var td = last.querySelectorAll("td");
    if (td.length < 3) return;
    writeText("previousSet", td[1].textContent.trim() + "kg × " + td[2].textContent.trim() +
      " · 第 " + td[3].textContent.trim() + " 组");
  }

  // ---------- Facts 卡片化（白底训练组卡：动作/重量/次数大字） ----------
  function mapFactsList() {
    var dst = $("facts-list");
    var tbl = $("facts-table");
    if (!dst || !tbl) return;
    var rows = tbl.querySelectorAll("tbody tr");
    var html = [];
    rows.forEach(function (tr, i) {
      var td = tr.querySelectorAll("td");
      if (td.length < 6) return;
      var weight = td[1].textContent.trim();
      var reps = td[2].textContent.trim();
      var prov = td[4].textContent.trim();
      html.push(
        '<div class="event-row">' +
        '<div class="event-num">' + (i + 1) + "</div>" +
        '<div class="event-main"><b>' + esc(td[0].textContent.trim()) + "</b>" +
        '<span>第 ' + esc(td[3].textContent.trim()) + " 组 · 重量来源 " + esc(prov) +
        (td[5].textContent.trim() ? " · 语音 " + esc(td[5].textContent.trim()) : "") + "</span></div>" +
        '<div class="event-tag">' + esc(weight) + "kg × " + esc(reps) + "</div></div>");
    });
    writeHtml("facts-list", html.join(""));
    var hasRows = rows.length > 0;
    var fe = $("facts-empty");
    if (fe) fe.classList.toggle("hidden", hasRows);
    dst.classList.toggle("hidden", !hasRows);
  }

  // ---------- 训练中记录列表（event-row 卡片化 + 组次计数） ----------
  function mapSetQueue() {
    var dst = $("set-queue");
    if (!dst) return;
    var rows = dst.querySelectorAll(".row");
    var html = [];
    rows.forEach(function (r, i) {
      var tag = r.querySelector(".tag");
      html.push(
        '<div class="event-row">' +
        '<div class="event-num">' + (i + 1) + "</div>" +
        '<div class="event-main"><b>' + r.innerHTML.replace(/<span class="tag[^"]*">.*?<\/span>/, "") + "</b></div>" +
        (tag ? '<div class="event-tag">' + esc(tag.textContent.trim()) + "</div>" : "") +
        "</div>");
    });
    writeHtml("set-queue", html.join(""));
    var rc = $("recordCount");
    if (rc && rows.length) rc.textContent = "已记录 " + rows.length + " 组";
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---------- 训练中派生展示（只展示，不产生任何业务数据） ----------
  function deriveActiveView() {
    var snap = window.FitcoreDemoInternals.snapshot
      ? window.FitcoreDemoInternals.snapshot() : null;
    var st = $("stateLabel");
    if (!st) return;

    // 上传进度 → 波形 live 动画 + 状态行
    var up = $("upload-progress");
    var wave = $("wave");
    var uploading = up && !up.classList.contains("hidden");
    if (wave) {
      var bar = $("upload-progress-bar");
      var pct = bar && bar.style.width ? parseInt(bar.style.width, 10) : 0;
      var active = uploading && pct > 0 && pct < 100;
      wave.classList.toggle("live", !!active);
    }

    // 状态行（job 进行中显示"识别中"，否则默认文案）
    if (uploading) {
      var t = $("upload-progress-text");
      if (t && t.textContent) {
        writeText("stateLabel", "● PROCESSING");
        st.style.color = "#f0a354";
        var sc = $("stateCopy");
        if (sc) writeText("stateCopy", t.textContent);
      }
    } else if (snap && snap.isLive) {
      writeText("stateLabel", "● LISTENING");
      st.style.color = "";
      var sc0 = $("stateCopy");
      if (sc0 && !sc0.dataset.userSet) writeText("stateCopy", "等待训练语音");
    }

    // 最新完成组（GET facts 权威）
    var saved = $("savedResult");
    if (saved && snap && snap.facts && snap.facts.length) {
      var f = snap.facts[snap.facts.length - 1];
      writeClass("savedResult", "hidden", false);
      var em = $("eventMain");
      if (em) writeText("eventMain", (f.weight_kg != null ? f.weight_kg + "kg × " + f.reps : "—"));
      var es = $("eventState");
      if (es) writeText("eventState", "已入账");
      // quoteBox 同步当前 Context
      var qb = $("quoteBox");
      if (qb) writeText("quoteBox", "当前：" + (f.exercise || "训练") + " · 已完成 " + snap.facts.length + " 组");
    } else if (saved) {
      writeClass("savedResult", "hidden", true);
    }
  }

  // ---------- 复盘概览：review-stats 文本 → 大数字 grid ----------
  function stylizeReviewStats() {
    var rs = $("review-stats");
    if (!rs) return;
    // app.js 写入纯文本（"总组数 n · 总次数 m · …"）；转换为母版 summary-grid
    var text = rs.textContent || "";
    if (rs.dataset.styled === text) return;
    rs.dataset.styled = text;
    var pairs = text.split("·").map(function (s) { return s.trim(); }).filter(Boolean);
    var items = [];
    pairs.forEach(function (p) {
      var m = p.match(/^(\S+)\s+(\S+)$/);
      if (m && !isNaN(parseFloat(m[2]))) {
        items.push("<div><small>" + esc(m[1]) + "</small><b>" + esc(m[2]) + "</b></div>");
      }
    });
    if (items.length) {
      rs.classList.add("summary-grid");
      writeHtml("review-stats", items.join(""));
    }
  }

  // ---------- 纠错卡片：标注「暂不可用」（与 app.js 语义一致） ----------
  function markCorrectionUnavailable() {
    var hint = $("correction-hint");
    var note = $("correction-unavailable");
    if (!note) return;
    if (hint && hint.textContent) {
      var p = note.querySelector("p");
      if (p) p.textContent = hint.textContent;
    }
  }

  // ---------- 同步调度：app.js 状态变化后 0ms 尾随（不侵入 app.js） ----------
  var scheduled = false;
  function scheduleSync() {
    if (scheduled) return;
    scheduled = true;
    Promise.resolve().then(function () {
      scheduled = false;
      try {
        syncFromAppDom();
        syncActiveNav(window.FitcoreDemoInternals.snapshot
          ? window.FitcoreDemoInternals.snapshot().view : "start");
      } catch (e) { /* 同步失败不影响业务 */ }
    });
  }

  function installDomObserver() {
    var target = document.body;
    var ob = new MutationObserver(function (muts) {
      var interesting = muts.some(function (m) {
        if (m.type !== "childList" && m.type !== "characterData") return false;
        var n = m.target;
        var id = n && n.id ? n.id : "";
        return id in ID_MAP || id === "fitcore-demo-data";
      });
      if (interesting) scheduleSync();
    });
    ob.observe(target, { subtree: true, childList: true, characterData: true });
  }

  // app.js 在 DOMContentLoaded 里 init；本补丁加载于其后，直接同步一次并挂观察器
  function boot() {
    bindNav();
    installDomObserver();
    scheduleSync();
    // 首页模式徽标可能由 app.js 异步刷新（isRealLive 判定），延迟再同步一次
    setTimeout(scheduleSync, 120);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
