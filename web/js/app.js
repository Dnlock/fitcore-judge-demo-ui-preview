/* Fitcore 演示前端 —— WS-E 评委产品化版（judge-demo）。
 *
 * 定位：面向评委的产品界面（非调试面板）。保留已验证的 HTTP Real Adapter
 * （FitcoreHTTPAdapter / httpAdapter.js），全部业务数据来自真实后端。
 *
 * 五页：
 *   1. 训练首页   —— Fitcore 品牌+定位、开始训练、简短使用说明、
 *                    当前演示模式状态徽标（测试ASR模式）、示例音频入口（预置 WAV 清单）。
 *   2. 训练进行中 —— 当前 Session、上传真实 WAV 或选择预置演示音频、上传进度、
 *                    Job 处理中状态（8 态轮询，202≠识别成功）、当前训练记录（只读 Facts）、
 *                    Pending 待确认区、结束训练按钮。
 *   3. 训练记录   —— 动作/重量/次数/组次/当前 Session，只来自 Core Facts 响应，前端零写死。
 *   4. Pending 确认—— 待确认内容、允许补充重量和次数、走正式 resolve 接口
 *                    （dismiss/confirm_set）、正确处理成功/冲突(409)/失败；
 *                    正式纠错 UI 未实现 → 明确标注"暂不可用"，不用假交互掩盖。
 *   5. 结束与复盘 —— Finish 请求（处理中/完成状态）、Review 读取（数字只来自后端响应）、
 *                    本次完成组、训练容量、返回首页/重新开始。
 *
 * 安全与模式：
 *   - 浏览器不暴露服务端 Bearer Token（维持代理持有 Token 架构：real_proxy.py）；
 *   - 模式切换不偷偷降级：REAL / MOCK 显式徽标；本产品以 REAL + 测试ASR 为主。
 *   - 页面刷新后 Session 恢复：localStorage 持久化 session_id + 游标，重载后
 *     经 GET session/facts/pending/finish/review 全量恢复。
 *
 * 本文件不实现真实 Fact Engine，不替代 Track A 入账判定。
 * 安全：不在浏览器源码内置任何 Token；真实远程 API 必须经受控服务端代理。
 */
(function () {
  "use strict";

  var CFG = {
    mode: "auto",   // auto = 先试 v1_sim 再回退；也可经 URL ?mode=mock|real|offline 显式指定
    mockV1Base: "http://127.0.0.1:8812/v1",
    // 受控服务端代理前缀（Real 模式，同源 /realproxy → 8814 real_proxy.py → 8790 真实 Core）
    realProxyBase: "/realproxy",
    apiBase: "/api/v1",
    pollMs: 500,
    pollIntervalMs: 600,
    autoAdvanceMs: 1600,
    // 客户端 WAV 预检约束（与真实 Core 一致；服务端 422 仍是最终裁决）
    wav: { maxBytes: 7000000, rate: 16000, channels: 1, sampwidth: 2 },
    // 8 态 Job 显示映射（真实 Core 状态机；202≠识别成功）
    jobStates: {
      queued: "排队中", asr_running: "语音识别中", final_ready: "结果就绪",
      llm_running: "语义理解中", retry_asr: "识别重试中", retry_llm: "语义重试中",
      blocked: "失败", done: "识别成功", uploading: "上传中",
    },
  };

  var state = {
    data: null,      // 内嵌演示剧本（由 index.html 注入）
    session: null,   // 运行时会话快照
    currentEvent: 0, // 已消费剧本事件数
    scene: "scene1",
    view: "start",
    replay: false,   // 离线回放模式（无真实 HTTP）
    mode: "auto",    // 本次会话实际生效模式: mock | real | offline
    finishBlocked: false, // Finish Barrier 409（pending 未决/缺序列）
    finishFailed: false,  // Finish 失败/未完成（非 barrier 原因）
    error: null,
    stats: { http: 0, replay: 0, retries: 0 },
    // ---- Phase 4 real 模式状态 ----
    sessionId: "",       // 服务端返回的实际 session_id（UUID）
    eventCursor: 0,      // events after 游标（event_seq）
    jobSeq: 0,           // 本会话已提交的 sequence 计数
    lastSeq: -1,         // 最后提交的 sequence（finish last_sequence 用）
    finishId: null,      // finish_id 幂等键（同 ID/范围重试）
    finishState: null,   // awaiting_audio | draining | finished
    review: null,        // ReviewRead 正式字段（finish 后 GET review）
    jobStatuses: {},     // uid → job（8 态实时）
    transcripts: [],     // TRANSCRIPT_FINAL 事件累积
    pendingTexts: {},    // pending.utterance_id → job final_text
    sourceAssurance: null, // 上传 202 的 source_assurance（服务端来源信任）
    uploads: {},         // uid → {name, size}（上传进度显示）
  };

  // ---------------------------------------------------------------- 工具
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function kv(o) { return Object.keys(o).map(function (k) { return k + "=" + o[k]; }).join("&"); }
  function uiBadge(t) { return '<span class="badge">' + esc(t) + "</span>"; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // URL 参数覆盖（运行时注入模式配置，代码内无 Token）
  function requestedMode() {
    try {
      var m = new URLSearchParams(window.location.search).get("mode");
      if (m === "real" || m === "mock" || m === "offline") return m;
    } catch (e) { /* file:// 或旧浏览器 */ }
    return CFG.mode;
  }

  // 演示模式：URL ?mode=real|mock|offline 显式指定；未指定时（auto）：
  // file://（离线单文件产物）→ offline 回放；http(s)（受控代理伺服）→ REAL 优先。
  function effectiveMode() {
    var want = requestedMode();
    if (want === "auto") {
      try {
        if (window.location.protocol === "file:") return "offline";
      } catch (e) { /* 无 location 环境 */ }
      return "real";
    }
    return want;
  }

  // ---------------------------------------------------------------- 状态持久化（localStorage）
  function persist() {
    try {
      localStorage.setItem("fitcore_demo_state", JSON.stringify({
        session: state.session, currentEvent: state.currentEvent,
        scene: state.scene, replay: state.replay, mode: state.mode,
        sessionId: state.sessionId, eventCursor: state.eventCursor,
        jobSeq: state.jobSeq, lastSeq: state.lastSeq, finishId: state.finishId,
        finishState: state.finishState, jobStatuses: state.jobStatuses,
        uploads: state.uploads,
        t: Date.now(),
      }));
    } catch (e) { /* 存储不可用时忽略 */ }
  }
  function restore() {
    try {
      var raw = localStorage.getItem("fitcore_demo_state");
      if (!raw) return false;
      var d = JSON.parse(raw);
      if (d && (d.session || d.sessionId)) {
        state.session = d.session;
        state.currentEvent = d.currentEvent | 0;
        state.scene = d.scene || "scene1";
        state.replay = !!d.replay;
        state.mode = d.mode || "mock";
        state.sessionId = d.sessionId || "";
        state.eventCursor = d.eventCursor | 0;
        state.jobSeq = d.jobSeq | 0;
        state.lastSeq = d.lastSeq != null ? d.lastSeq : -1;
        state.finishId = d.finishId || null;
        state.finishState = d.finishState || null;
        state.jobStatuses = d.jobStatuses || {};
        state.uploads = d.uploads || {};
        return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  }
  function wipe() {
    try { localStorage.removeItem("fitcore_demo_state"); } catch (e) { /* ignore */ }
  }

  // ---------------------------------------------------------------- 传输层
  function makeV1(mode) {
    if (mode === "real") {
      if (typeof FitcoreHTTPAdapter === "undefined") return null;
      return new FitcoreHTTPAdapter({
        mode: "real",
        base: CFG.realProxyBase + "/v1",
        sessionId: state.sessionId || "",   // 创建会话后由服务端返回实际 ID
        log: function () {},
      });
    }
    var base = CFG.mockV1Base;
    if (typeof FitcoreHTTPAdapter !== "undefined") {
      return new FitcoreHTTPAdapter({
        mode: "mock",
        base: base,
        sessionId: "S-0001",
        log: function () {},
      });
    }
    if (typeof FitcoreV1Client === "undefined") return null;
    return new FitcoreV1Client({
      base: base,
      sessionId: "S-0001",
      log: function () {},
    });
  }

  // 真实 HTTP 客户端（含 real 模式头）——暴露给测试断言
  window.FitcoreDemoInternals = {
    makeV1: makeV1,
    requestedMode: requestedMode,
    effectiveMode: effectiveMode,
    reviewSummary: reviewSummary,
    reviewAvailable: reviewAvailable,
    sessionDone: sessionDone,
    readWavInfo: readWavInfo,
    modeBadgeText: modeBadgeText,
    resolvePendingRaw: resolvePendingRaw,
    snapshot: function () {
      return {
        mode: state.mode,
        isLive: !!(state.v1 && state.v1.isLive),
        sessionId: state.sessionId,
        view: state.view,
        sessionStatus: state.session ? state.session.status : null,
        sessionVersion: state.session ? state.session.version : null,
        pending: (state.session && state.session.pending || []).map(function (p) {
          return { id: p.id, status: p.status, reason: p.reason };
        }),
        facts: (state.session && state.session.facts || []).map(function (f) {
          return { id: f.id, exercise: f.exercise, weight_kg: f.weight_kg, reps: f.reps };
        }),
      };
    },
    CFG: CFG,
  };
  window.__JUDGE_TRACE__ = [];

  function api(method, path, body) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open(method, CFG.apiBase + path, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if (xhr.status >= 200 && xhr.status < 300) {
          try { resolve(JSON.parse(xhr.responseText)); }
          catch (e) { reject(new Error("响应不是合法 JSON")); }
        } else {
          var msg = "HTTP " + xhr.status;
          try { msg = JSON.parse(xhr.responseText).error.message || msg; } catch (e) { /* ignore */ }
          reject(new Error(msg));
        }
      };
      xhr.onerror = function () { reject(new Error("网络不可达")); };
      xhr.send(body ? JSON.stringify(body) : null);
    });
  }

  function apiWithRetry(method, path, body, tries) {
    tries = tries || 1;
    state.stats.http++;
    return api(method, path, body).catch(function (err) {
      state.stats.retries++;
      if (tries > 1) return apiWithRetry(method, path, body, tries - 1);
      throw err;
    });
  }

  // 离线回放：把剧本事件按演示时序映射为"本地会话"（MOCK 展示层，非真实入账引擎）
  function replayEvent(ev) {
    var s = state.session;
    if (s.events.indexOf(ev.id) >= 0) return; // 幂等
    s.events.push(ev.id);
    if (ev.t === "say") {
      s.jobs.push({
        job_id: ev.job || ("JOB-OFF-" + ev.id),
        event_id: ev.id, status: "processed", mock_audio: true,
      });
    }
    (ev.facts || []).forEach(function (f) {
      var dup = s.facts.some(function (x) {
        return x.movement === f[0] && x.weight_kg === f[1] && x.reps === f[2] && !!x.approx === !!f[3].approx;
      });
      if (dup) return; // 重复事件不重复展示训练组
      s.facts.push({
        id: "F-" + String(s.facts.length + 1).padStart(4, "0"),
        movement: f[0], weight_kg: f[1], reps: f[2],
        status: f[3].explicit ? "confirmed" : "pending",
        approx: !!f[3].approx,
        cross_statement: !!f[3].cross_statement,
        corrected: false, correction_ids: [], dup_events: [],
      });
    });
    if (ev.rir != null) {
      s.pending.push({
        id: "P-" + String(s.pending.length + 1).padStart(4, "0"),
        kind: "rir", value: ev.rir, status: "open",
        note: "RIR 提示：不增加已完成次数",
      });
    }
    if (s.status === "recording") s.status = "active";
  }

  // ---------------------------------------------------------------- 真实契约映射（Phase 4）
  // ContextRead → 会话快照（id 用真实 session_id）
  function mapContext(d) {
    return {
      id: d.session_id, status: d.status, version: d.version,
      active_exercise_name: d.active_exercise_name || null,
      active_exercise_instance_id: d.active_exercise_instance_id != null ? d.active_exercise_instance_id : null,
      last_completed_set_id: d.last_completed_set_id != null ? d.last_completed_set_id : null,
      last_reported_weight: d.last_reported_weight != null ? d.last_reported_weight : null,
      next_set_plan: d.next_set_plan != null ? d.next_set_plan : null,
      facts: [], pending: [], corrections: [], events: [], jobs: [],
    };
  }
  // FactRead（真实字段）→ 视图事实（全部为权威 confirmed；无 approx 字段）
  function mapFact(f) {
    return {
      id: f.id, fid: f.id,
      movement: f.exercise, exercise: f.exercise,
      set_number: f.set_number, weight_kg: f.weight_kg, reps: f.reps,
      weight_provenance: f.weight_provenance,
      weight_source_fact_id: f.weight_source_fact_id,
      source_utterance_id: f.source_utterance_id,
      exercise_instance_id: f.exercise_instance_id,
      status: "confirmed", real: true,
    };
  }
  // PendingRead（真实字段）→ 视图 pending 项
  function mapPending(p) {
    return {
      id: p.id, pid: p.id,
      utterance_id: p.utterance_id, reason: p.reason,
      status: p.status,   // open | resolved | dismissed
      context_version: p.context_version_at_defer,
      exercise_instance_id: p.exercise_instance_id,
      last_completed_set_id: p.last_completed_set_id,
      real: true,
    };
  }

  // 真实 Core 事件 → UI 反馈（FACT_COMMITTED 不计数，Facts 以 GET facts 为准）
  function handleRealEvent(evx) {
    switch (evx.type) {
      case "TRANSCRIPT_FINAL":
        state.transcripts.push({ seq: evx.event_seq, uid: evx.utterance_id,
                                 text: evx.payload && evx.payload.text });
        break;
      case "FACT_COMMITTED":
        // 权威组数以 GET facts 为准；此处仅记录，不做任何计数
        state.transcripts.push({ seq: evx.event_seq, type: "FACT_COMMITTED",
                                 scope: evx.payload && evx.payload.event_scope,
                                 ctx_version: evx.payload && evx.payload.context_version });
        break;
      case "OPERATION_DEFERRED":
        state.transcripts.push({ seq: evx.event_seq, type: "OPERATION_DEFERRED",
                                 reason: evx.payload && evx.payload.reason });
        break;
      case "INPUT_REJECTED":
        state.transcripts.push({ seq: evx.event_seq, type: "INPUT_REJECTED",
                                 reason: evx.payload && evx.payload.reason });
        break;
      case "PROCESSING_ERROR":
        state.transcripts.push({ seq: evx.event_seq, type: "PROCESSING_ERROR",
                                 stage: evx.payload && evx.payload.stage,
                                 error_type: evx.payload && evx.payload.error_type });
        break;
    }
  }

  // real 模式全量刷新（页面重载恢复 + 每次作业后同步）
  // 并发防抖：只应用最新一次刷新结果（旧响应不覆盖新状态）
  state.refreshSeq = 0;
  function refreshReal() {
    if (!state.v1 || !state.v1.isLive || state.mode !== "real") return Promise.resolve();
    var v1 = state.v1.client;
    var seq = ++state.refreshSeq;
    return Promise.all([
      v1.getSession().then(function (r) { return mapContext(r.data); })
        .catch(function () { return null; }),
      v1.getFacts().then(function (r) { return (r.data || []).map(mapFact); })
        .catch(function () { return null; }),
      v1.getPending().then(function (r) { return (r.data || []).map(mapPending); })
        .catch(function () { return null; }),
    ]).then(function (parts) {
      if (seq !== state.refreshSeq) return; // 过期刷新：丢弃（不覆盖新状态）
      // 字段级合并：不互相覆盖（getSession 的 ContextRead 不含 facts/pending）
      var ctx = parts[0];
      if (ctx) {
        state.session = state.session || {};
        for (var k in ctx) state.session[k] = ctx[k];
      }
      if (parts[1]) state.session.facts = parts[1];
      if (parts[2]) state.session.pending = parts[2];
      // pending 关联的 job final_text（用于确认提示）
      var jobs = (state.session.pending || []).filter(function (p) { return p.status === "open"; });
      return Promise.all(jobs.map(function (p) {
        return v1.getJob(p.utterance_id).then(function (r) {
          var j = r.data.job || {};
          state.pendingTexts[p.utterance_id] = j.final_text || null;
          state.jobStatuses[p.utterance_id] = j;
        }).catch(function () {});
      }));
    });
  }

  // ---------------------------------------------------------------- WAV 读取与客户端预检
  // 只读 RIFF 头（16kHz/单声道/PCM16/≤7MB），与服务端约束一致；
  // 服务端 422 INVALID_COMPLETE_WAV 仍是最终裁决（静音/DC 由服务端判定）。
  function readWavInfo(buf) {
    // 跨 realm 稳健：接受任何带数值 length 与下标访问的字节序列
    // （浏览器 FileReader→Uint8Array、jsdom 测试注入的 Node Uint8Array 均可）
    if (!buf || typeof buf.length !== "number" || buf.length < 12) {
      return { ok: false, reason: "文件过小，不是合法 WAV" };
    }
    var txt = "";
    for (var i = 0; i < 4; i++) txt += String.fromCharCode(buf[i]);
    if (txt !== "RIFF") return { ok: false, reason: "不是 WAV 文件（缺 RIFF 头）" };
    var wave = "";
    for (var j = 8; j < 12; j++) wave += String.fromCharCode(buf[j]);
    if (wave !== "WAVE") return { ok: false, reason: "不是 WAV 文件（缺 WAVE 头）" };
    // 扫描 fmt/data 块
    var rate = 0, channels = 0, bits = 0;
    var pos = 12;
    while (pos + 8 <= buf.length) {
      var id = "";
      for (var k = pos; k < pos + 4; k++) id += String.fromCharCode(buf[k]);
      var size = buf[pos + 4] | (buf[pos + 5] << 8) | (buf[pos + 6] << 16) | (buf[pos + 7] << 24);
      if (id === "fmt ") {
        if (pos + 8 + 16 > buf.length) break;
        var fmt = pos + 8;
        var tag = buf[fmt] | (buf[fmt + 1] << 8);
        channels = buf[fmt + 2] | (buf[fmt + 3] << 8);
        rate = buf[fmt + 4] | (buf[fmt + 5] << 8) | (buf[fmt + 6] << 16) | (buf[fmt + 7] << 24);
        bits = buf[fmt + 14] | (buf[fmt + 15] << 8);
        if (tag !== 1) return { ok: false, reason: "只支持 PCM 编码" };
      }
      pos += 8 + size + (size % 2); // 块按 2 字节对齐
      if (id === "data") break;
    }
    if (rate !== CFG.wav.rate) return { ok: false, reason: "采样率需为 16000Hz（实际 " + rate + "Hz）" };
    if (channels !== CFG.wav.channels) return { ok: false, reason: "需为单声道（实际 " + channels + " 声道）" };
    if (bits !== CFG.wav.sampwidth * 8) return { ok: false, reason: "需为 PCM16（实际 " + bits + "bit）" };
    return { ok: true, rate: rate, channels: channels, bits: bits, bytes: buf.length };
  }

  function base64FromBytes(bytes) {
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  // ---------------------------------------------------------------- 音频作业提交
  function setUploadProgress(uid, pct, text) {
    var bar = $("upload-progress-bar");
    var t = $("upload-progress-text");
    if (pct != null && bar) {
      bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
      $("upload-progress").classList.remove("hidden");
    }
    if (text != null && t) t.textContent = text;
    if (uid != null) {
      state.uploads[uid] = state.uploads[uid] || {};
      if (pct != null) state.uploads[uid].pct = pct;
      if (text != null) state.uploads[uid].text = text;
      persist();
    }
  }

  // 公共提交入口：返回 job（完成/失败后）；不处理端到端渲染
  function submitAudio(bytes, name, uid) {
    var v1 = state.v1.client;
    var seq = state.jobSeq++;
    state.lastSeq = seq;
    var b64 = base64FromBytes(bytes);
    setUploadProgress(uid, 10, "编码完成，提交中…");
    return v1.submitAudioJob(b64, { uid: uid, sequence: seq, audioSource: "client_complete_wav" })
      .then(function (r) {
        // 202 = 已受理（acceptance），不等于识别成功 —— 必须继续轮询到 done/blocked
        var j = r.data.job || {};
        state.jobStatuses[uid] = j;
        state.uploads[uid] = state.uploads[uid] || { name: name, bytes: bytes.length };
        state.uploads[uid].accepted = true;
        state.uploads[uid].status = r.status;
        state.sourceAssurance = r.data.source_assurance || null;
        setUploadProgress(uid, 40, "已受理（" + r.status + " acceptance=" + (r.data.acceptance || "?") + "），等待后端处理…");
        persist();
        return v1.pollJob(uid, function (status, job) {
          state.jobStatuses[uid] = state.jobStatuses[uid] || {};
          state.jobStatuses[uid].status = status;
          if (job) {
            state.jobStatuses[uid].final_text = job.final_text;
            state.jobStatuses[uid].asr_attempts = job.asr_attempts;
            state.jobStatuses[uid].error_type = job.error_type;
          }
          setUploadProgress(uid, 45 + 50 * progressOf(status), "处理中（" + jobLabel(status) + "）…");
          renderJobList();
        }, CFG.pollMs, 120);
      })
      .then(function (job) {
        state.jobStatuses[uid] = job || state.jobStatuses[uid];
        state.pendingTexts[uid] = job && job.final_text;
        if (job && job.status === "blocked") {
          setUploadProgress(uid, 100, "识别失败（blocked" + (job.error_type ? "：" + job.error_type : "") + "）—— 后端不伪造事实，Facts 无新增。");
          setError("音频作业 " + uid + " 最终失败（blocked）。真实后端安全行为：不伪造事实，Facts 无新增。");
        } else if (job && job.status === "done") {
          setUploadProgress(uid, 100, "识别成功（done）" + (job.final_text ? "：「" + job.final_text + "」" : ""));
          setError(null);
        } else {
          setUploadProgress(uid, 100, "作业结束（" + jobLabel(job && job.status) + "）");
        }
        persist();
        return job;
      })
      .then(function (job) {
        // events 增量同步（after/limit 分页，断线恢复）
        return v1.drainEvents(handleRealEvent, state.eventCursor).then(function (c) {
          if (c != null) state.eventCursor = c;
          persist();
          return job;
        });
      })
      .then(function (job) { return refreshReal().then(function () { return job; }); })
      .catch(function (err) {
        setUploadProgress(uid, 100, "失败：" + ((err && (err.code || err.message)) || "未知错误"));
        // real 不静默降级：保持错误态
        setError("真实链路失败：" + ((err && (err.code || err.message)) || "未知错误") +
                 "（real 模式不自动回退演示数据）");
        throw err;
      });
  }

  function progressOf(status) {
    var order = ["queued", "asr_running", "final_ready", "llm_running", "retry_asr", "retry_llm", "blocked", "done"];
    var i = order.indexOf(status);
    return i < 0 ? 0.2 : (i + 1) / (order.length + 1);
  }

  function jobLabel(status) {
    return CFG.jobStates[status] || (status || "未知");
  }

  // ---------------------------------------------------------------- 会话生命周期
  function startSession(scene) {
    state.scene = scene;
    var seed = state.data.scenes[scene];
    state.v1 = null;      // 每场景重置 /v1 客户端状态
    state.eventCursor = 0;
    state.mode = "mock";
    state.finishBlocked = false;
    state.finishFailed = false;
    state.finishState = null;
    state.review = null;
    state.jobStatuses = {};
    state.transcripts = [];
    state.pendingTexts = {};
    state.jobSeq = 0;
    state.lastSeq = -1;
    state.finishId = null;
    state.sessionId = "";
    state.sourceAssurance = null;
    state.uploads = {};
    var want = effectiveMode();
    if (want === "offline") {
      return enterReplay(seed);
    }
    var v1 = makeV1(want);
    if (v1) {
      if (want === "real") {
        // 真实契约：POST /v1/sessions（body 可空）→ 201，session_id 为服务端实际 UUID
        return v1.createSession()
          .then(function (r) {
            var d = r.data;
            state.v1 = { client: v1, isLive: true };
            state.mode = "real";
            state.sessionId = d.session_id;
            v1.setSessionId(d.session_id);
            state.session = mapContext(d);
            state.replay = false;
            persist();
            render();
            return refreshReal();
          })
          .catch(function (err) {
            // Real 模式失败：不静默降级（防止把演示数据误当真实数据）
            state.session = { id: "REAL-ERROR", status: "error", scene: scene,
                              facts: [], pending: [], corrections: [], events: [], jobs: [],
                              _offline: false };
            state.replay = false;
            state.mode = "real";
            setError("Real 模式不可用：真实后端代理（/realproxy → 8790）未就绪或缺少 REAL_API_TOKEN。已保持 real 模式，请检查代理后再试（不自动回退演示数据）。错误：" +
                     ((err && (err.code || err.message)) || "未知"));
            render();
          });
      }
      // mock：优先 /v1 契约（v1_sim CONTRACT_SIM，Phase2 旧契约）
      return v1.createSession({ scene: scene, reset: true })
        .then(function (r) {
          state.v1 = { client: v1, isLive: true };
          state.session = r.data;
          state.replay = false;
          state.mode = want === "real" ? "real" : "mock";
        })
        .catch(function () {
          if (want === "real") {
            // Real 模式失败：不静默降级（防止把演示数据误当真实数据）
            state.session = { id: "S-0001", status: "error", scene: scene,
                              facts: [], pending: [], corrections: [], events: [], jobs: [],
                              _offline: true };
            state.replay = false;
            state.mode = "real";
            setError("Real 模式不可用：真实后端代理（/realproxy, 8814）未就绪或缺少 REAL_API_TOKEN。已保持 real 模式，请检查代理后再试（不自动回退演示数据）。");
            return;
          }
          state.stats.replay++;
          state.replay = true;
          state.mode = "offline";
          state.session = {
            id: "S-0001", status: "recording", scene: scene,
            facts: [], pending: [], corrections: [], events: [], jobs: [],
            _offline: true,
          };
          if (seed.sess) replayEvent(seed.sess);
        });
    }
    var body = { scene: scene, reset: true };
    return apiWithRetry("POST", "/sessions", body, 1)
      .then(function (res) { state.session = res; state.replay = false; })
      .catch(function () {
        state.stats.replay++;
        state.replay = true;
        state.mode = "offline";
        state.session = {
          id: "S-0001", status: "recording", scene: scene,
          facts: [], pending: [], corrections: [], events: [], jobs: [],
          _offline: true,
        };
        if (seed.sess) replayEvent(seed.sess);
      })
      .then(function () {
        state.currentEvent = 0;
        persist();
        render();
      });
  }

  function enterReplay(seed) {
    state.replay = true;
    state.mode = "offline";
    state.session = {
      id: "S-0001", status: "recording", scene: state.scene,
      facts: [], pending: [], corrections: [], events: [], jobs: [],
      _offline: true,
    };
    if (seed.sess) replayEvent(seed.sess);
    state.currentEvent = 0;
    persist();
    render();
    return Promise.resolve();
  }

  function nextEvent() {
    var seed = state.data.scenes[state.scene];
    var ev = seed.events[state.currentEvent];
    if (!ev) return Promise.resolve();
    state.currentEvent++;
    persist();
    if (state.mode !== "real" || !state.v1 || !state.v1.isLive) {
      if (state.replay) {
        replayEvent(ev);
        return new Promise(function (r) { setTimeout(function () { render(); r(); }, 160); });
      }
      var body = ev.event_id ? { event_id: ev.event_id } : { audio_ref: "AUD-" + ev.id };
      return apiWithRetry("POST", "/sessions/S-0001/audio-jobs", body, 1)
        .catch(function (err) {
          // 网络失败：进入离线回放并继续
          state.replay = true;
          state.session._offline = true;
          state.stats.replay++;
          replayEvent(ev);
        })
        .then(function () { render(); });
    }

    // ---------- real 模式：multipart 上传 → 8 态轮询 → events 增量 → facts/pending 刷新
    var v1 = state.v1.client;
    if (ev.t === "end") {
      // 剧本结束事件：仅提示；真实 finish 由「结束训练」按钮（FitcoreDemo.end）触发
      return refreshReal().then(function () { render(); });
    }
    var audioB64 = state.data.realWavB64 && state.data.realWavB64[ev.id];
    if (!audioB64) {
      setError("缺少真实模式确定性测试 WAV（fixture 未注入）：" + ev.id + "（测试 Provider 条件，非真实人声）");
      return Promise.resolve();
    }
    var uid = ev.job || ("U-" + Date.now().toString(36));   // utterance_id：同 session 内幂等主键
    var bytes = Uint8Array.from(atob(audioB64), function (c) { return c.charCodeAt(0); });
    var name = (state.data.realWavFiles && state.data.realWavFiles[ev.id]) || (ev.id + ".wav");
    return submitAudio(bytes, name, uid)
      .then(function () { render(); });
  }

  function refreshAll() {
    if (state.mode === "real" && state.v1 && state.v1.isLive) {
      var v1 = state.v1.client;
      // 同步 finish 状态与 review（若已完成）
      return refreshReal().then(function () {
        return v1.getFinish().then(function (r) {
          state.finishState = r.data.state;
          if (r.data.complete) {
            state.session.status = "finished";
            return v1.getReview().then(function (rr) { state.review = rr.data; })
              .catch(function () {});
          }
        }).catch(function () { /* 404 = 未请求 finish：正常 */ });
      }).then(function () { render(); });
    }
    if (state.replay || !state.session || state.session._offline || !state.v1 || !state.v1.isLive) {
      render();
      return Promise.resolve();
    }
    var v1 = state.v1.client;
    return Promise.all([
      v1.getFacts().then(function (r) { state.session.facts = r.data.facts; }).catch(function () {}),
      v1.getPending().then(function (r) { state.session.pending = r.data.pending; }).catch(function () {}),
    ]).then(function () { render(); });
  }

  // ---------------------------------------------------------------- 操作
  function resolveFact(fid, decision) {
    state.stats.http++;
    if (state.replay || state.session._offline) {
      var f = state.session.facts.find(function (x) { return x.id === fid; });
      if (f && f.status === "pending") f.status = decision === "confirm" ? "confirmed" : "hidden";
      persist(); render();
      return Promise.resolve();
    }
    // /v1 客户端路径：真实契约经 pending/{pid}/resolve（decision: accept/dismiss）；
    // 找到该事实对应的 pending 项转发语义；兼容实现提供 facts/{fid}/resolve。
    var v1 = state.v1 && state.v1.isLive ? state.v1.client : null;
    if (v1 && typeof v1.resolvePending === "function") {
      var p = (state.session.pending || []).find(function (x) {
        return x.kind === "fact" && x.fact_id === fid && x.status === "open";
      });
      if (p) {
        return v1.resolvePending(p.id, decision === "confirm" ? "accept" : "dismiss")
          .then(function (r) {
            var i = state.session.pending.findIndex(function (x) { return x.id === p.id; });
            if (i >= 0) state.session.pending[i] = r.data;
            return v1.getFacts();
          })
          .then(function (fr) { state.session.facts = fr.data.facts; persist(); render(); })
          .catch(function (err) { setError("确认失败：" + err.message); render(); });
      }
      if (typeof v1.resolveFact === "function") {
        return v1.resolveFact(fid, decision)
          .then(function (r) {
            var i = state.session.facts.findIndex(function (x) { return x.id === fid; });
            if (i >= 0) state.session.facts[i] = r.data;
            persist(); render();
          })
          .catch(function (err) { setError("确认失败：" + err.message); render(); });
      }
    }
    return apiWithRetry("POST", "/sessions/S-0001/facts/" + fid + "/resolve", { decision: decision }, 1)
      .then(function (f) {
        var i = state.session.facts.findIndex(function (x) { return x.id === fid; });
        if (i >= 0) state.session.facts[i] = f;
        persist(); render();
      }).catch(function (err) { setError("确认失败：" + err.message); render(); });
  }

  function correctFact(fid, field, value) {
    state.stats.http++;
    var body = {}; body[field] = Number(value);
    if (state.replay || state.session._offline) {
      var f = state.session.facts.find(function (x) { return x.id === fid; });
      if (f) {
        f[field] = Number(value); f.corrected = true;
        state.session.corrections.push({ id: "C-LOCAL", fact_id: fid, changes: body });
      }
      persist(); render();
      return Promise.resolve();
    }
    if (state.mode === "real") {
      // 真实 Core 4797485 无 facts/{fid}/correction 路由：明确标注未接线，不伪造
      setError("真实 Core 4797485 未接线事实纠错路由（14 条正式路由无 correction）。纠错语义由 CORRECTION 语音经 Fact Engine 承担；本视图在 Real 模式只读展示。");
      render();
      return Promise.resolve();
    }
    // Correction 按 Fact ID 更新（POST facts/{fid}/correction，不新增组）——v1_sim 兼容
    if (state.v1 && state.v1.isLive && typeof state.v1.client.correctFact === "function") {
      return state.v1.client.correctFact(fid, body)
        .then(function (r) {
          var i = state.session.facts.findIndex(function (x) { return x.id === fid; });
          if (i >= 0) state.session.facts[i] = r.data;
          persist(); render();
        }).catch(function (err) { setError("纠错失败：" + err.message); render(); });
    }
    return apiWithRetry("POST", "/sessions/S-0001/facts/" + fid + "/correction", body, 1)
      .then(function (f) {
        var i = state.session.facts.findIndex(function (x) { return x.id === fid; });
        if (i >= 0) state.session.facts[i] = f;
        persist(); render();
      }).catch(function (err) { setError("纠错失败：" + err.message); render(); });
  }

  function resolvePending(pid, decision, weightKg, reps) {
    state.stats.http++;
    // 决策值透传：L-02 正式 dismiss/confirm_set；旧值 accept 由服务端/离线路径兼容裁决
    if (state.replay || state.session._offline) {
      var p0 = state.session.pending.find(function (x) { return x.id === pid; });
      if (p0 && p0.status === "open") {
        p0.status = decision === "confirm_set" ? "confirmed"
          : decision === "accept" ? "accepted" : "dismissed";
        if (p0.kind === "fact" && p0.fact_id) {
          var f0 = state.session.facts.find(function (x) { return x.id === p0.fact_id; });
          if (f0) f0.status = (decision === "confirm_set" || decision === "accept") ? "confirmed" : "hidden";
        }
      }
      persist(); render();
      return Promise.resolve();
    }
    if (state.v1 && state.v1.isLive && state.mode === "real") {
      var pend = (state.session.pending || []).find(function (x) {
        return String(x.id) === String(pid); // 宽松比较：DOM dataset 是字符串，后端 id 可能是数字
      });
      if (!pend) return Promise.resolve();
      var req = {
        resolution_id: "R-" + Date.now().toString(36),
        action: decision === "confirm_set" ? "confirm_set" : "dismiss",
        // 服务端要求 context_version = 当前会话版本（否则 STALE_CONTEXT）
        context_version: state.session.version != null ? state.session.version : 0,
      };
      state.resolutions = state.resolutions || {};
      state.resolutions[pid] = req.resolution_id;
      if (decision === "confirm_set") {
        // 完整确认：weight_kg + reps 必须提供（服务端 FULL_SET_CONFIRMATION_REQUIRED）
        var wEl = $("pend-weight-" + pid);
        var rEl = $("pend-reps-" + pid);
        var w = weightKg != null ? Number(weightKg) : (wEl ? Number(wEl.value) : NaN);
        var r = reps != null ? Number(reps) : (rEl ? Number(rEl.value) : NaN);
        if (!isFinite(w) || w <= 0) { showResolveResult(pid, "error", "确认入账需要有效重量（kg）"); setError("确认入账需要有效重量（kg）"); render(); return Promise.resolve(); }
        if (!isFinite(r) || r <= 0) { showResolveResult(pid, "error", "确认入账需要有效次数"); setError("确认入账需要有效次数"); render(); return Promise.resolve(); }
        req.weight_kg = w;
        req.reps = Math.round(r);
      }
      showResolveResult(pid, "pending", "正在提交（" + (decision === "confirm_set" ? "确认入账" : "忽略") + "）…");
      return state.v1.client.resolvePending(pid, req)
        .then(function (r) {
          var i = state.session.pending.findIndex(function (x) { return x.id === pid; });
          if (i >= 0) state.session.pending[i].status = "closed";
          showResolveResult(pid, "ok",
            "处理成功（" + esc((r.data && r.data.decision) || "RESOLVED") + "）" +
            (r.data && r.data.committed && r.data.committed.length
              ? "：已入账 " + r.data.committed.length + " 组" : ""));
          setError(null);
          // Correction 后的刷新：Facts/Pending/Context 全量回读
          return refreshReal().then(function () { persist(); render(); return r; });
        })
        .catch(function (err) {
          var st = err && err.status;
          var code = err && err.code;
          if (st === 409) {
            // 冲突：明确展示后端拒绝原因（不静默吞掉）
            var hint = code === "STALE_CONTEXT" ? "会话已更新，请刷新后重试"
              : code === "FULL_SET_CONFIRMATION_REQUIRED" ? "确认入账必须提供重量与次数"
              : code === "PENDING_NOT_CONFIRMABLE_AS_SET" ? "该待确认项不能按训练组入账（可忽略）"
              : code === "NEW_SET_SINCE_PENDING" ? "待确认期间产生了新训练组，请重新处理"
              : code === "RIR_CANNOT_CONFIRM_SET" ? "余力提示不能作为训练组入账"
              : "状态冲突（" + (code || "409") + "）";
            showResolveResult(pid, "error", "处理冲突（409）：" + hint);
            setError("处理冲突（409）：" + hint);
          } else {
            showResolveResult(pid, "error", "处理失败：" + ((err && (err.code || err.message)) || "未知错误"));
            setError("处理失败：" + ((err && (err.code || err.message)) || "未知错误"));
          }
          render();
        });
    }
    if (state.v1 && state.v1.isLive) {
      return state.v1.client.resolvePending(pid, decision)
        .then(function (r) {
          var i = state.session.pending.findIndex(function (x) { return x.id === pid; });
          if (i >= 0) state.session.pending[i] = r.data;
          if (r.data.kind === "fact" && r.data.fact_id) {
            var j = state.session.facts.findIndex(function (x) { return x.id === r.data.fact_id; });
            if (j >= 0) {
              state.session.facts[j].status =
                (decision === "confirm_set" || decision === "accept") ? "confirmed" : "hidden";
            }
          }
          persist(); render();
        }).catch(function (err) { setError("处理失败：" + err.message); render(); });
    }
    return apiWithRetry("POST", "/sessions/S-0001/pending/" + pid + "/resolve", { decision: decision }, 1)
      .then(function (p) {
        var i = state.session.pending.findIndex(function (x) { return x.id === pid; });
        if (i >= 0) state.session.pending[i] = p;
        persist(); render();
      }).catch(function (err) { setError("处理失败：" + err.message); render(); });
  }

  function showResolveResult(pid, kind, text) {
    var el = $("resolve-result");
    if (el) {
      el.classList.remove("hidden", "ok", "error", "pending");
      el.classList.add(kind);
      el.innerHTML = "<b>Pending #" + esc(pid) + "</b> · " + esc(text);
    }
  }

  // 诊断钩子（jsdom 测试用）：按真实契约直接提交 resolve 请求，验证
  // 成功 RESOLVED / 幂等 DUPLICATE / 冲突 409 / 网络失败四类结果处理。
  function resolvePendingRaw(pid, req) {
    if (!(state.v1 && state.v1.isLive && state.mode === "real")) {
      return Promise.resolve({ status: 0, data: { decision: "OFFLINE" } });
    }
    state.resolutions = state.resolutions || {};
    if (state.resolutions[pid] && !req.resolution_id) {
      req.resolution_id = state.resolutions[pid]; // 复用上次 resolution_id（服务端 DUPLICATE 幂等裁决）
    }
    showResolveResult(pid, "pending", "正在提交（" + esc(req.action || "?") + "）…");
    return state.v1.client.resolvePending(pid, req)
      .then(function (r) {
        showResolveResult(pid, "ok",
          "处理成功（" + esc((r.data && r.data.decision) || "RESOLVED") + "）" +
          (r.data && r.data.committed && r.data.committed.length
            ? "：已入账 " + r.data.committed.length + " 组" : ""));
        setError(null);
        return refreshReal().then(function () { persist(); render(); return r; });
      })
      .catch(function (err) {
        var st = err && err.status;
        var code = err && err.code;
        if (st === 409) {
          var hint = code === "STALE_CONTEXT" ? "会话已更新，请刷新后重试"
            : code === "FULL_SET_CONFIRMATION_REQUIRED" ? "确认入账必须提供重量与次数"
            : code === "PENDING_NOT_CONFIRMABLE_AS_SET" ? "该待确认项不能按训练组入账（可忽略）"
            : code === "NEW_SET_SINCE_PENDING" ? "待确认期间产生了新训练组，请重新处理"
            : code === "RIR_CANNOT_CONFIRM_SET" ? "余力提示不能作为训练组入账"
            : "状态冲突（" + (code || "409") + "）";
          showResolveResult(pid, "error", "处理冲突（409）：" + hint);
          setError("处理冲突（409）：" + hint);
        } else {
          showResolveResult(pid, "error", "处理失败：" + ((err && (err.code || err.message)) || "未知错误"));
          setError("处理失败：" + ((err && (err.code || err.message)) || "未知错误"));
        }
        render();
        throw err;
      });
  }

  function endSession() {
    state.stats.http++;
    setFinishStatus("正在请求结束训练（Finish 请求）…");
    if (state.mode === "real" && state.v1 && state.v1.isLive) {
      // 真实 Finish 屏障事务：{finish_id, last_sequence}；200 complete / 202 draining / 409 awaiting_audio
      var v1 = state.v1.client;
      var fin = {
        finish_id: state.finishId || ("F-" + Date.now().toString(36)),
        last_sequence: state.lastSeq >= 0 ? state.lastSeq : -1,
      };
      state.finishId = fin.finish_id;
      return v1.finish(fin).then(function (r) {
        state.finishState = r.data.state;
        if (r.status === 200 || r.data.complete) {
          state.session.status = "finished";
          state.finishBlocked = false; state.finishFailed = false;
          setFinishStatus("训练已结束（服务端确认完成）");
          return v1.getReview().then(function (rr) { state.review = rr.data; })
            .catch(function () {})
            .then(function () { persist(); render(); });
        }
        if (r.status === 202) {
          // draining：等待全部 job done 后服务端自动闭合 → 轮询 GET finish
          state.session.status = "draining";
          setFinishStatus("训练结束处理中（服务端等待剩余语音作业完成，自动闭合）…");
          return pollFinish(v1, fin);
        }
        persist(); render();
      }).catch(function (err) {
        if (err && err.status === 409) {
          state.finishState = (err.data && err.data.state) || "awaiting_audio";
          state.finishBlocked = true;
          state.finishFailed = false;
          state.session.status = "active";
          setFinishStatus("结束被阻止（409 awaiting_audio）：存在未完成/缺失序列的音频作业。请等待作业完成或补传缺失序列后，以相同 finish_id 重试（幂等）。");
          setError("Finish Barrier（409）：存在未完成/缺失序列的音频作业（awaiting_audio）。请补传缺失序列后以相同 finish_id 重试（幂等）。");
        } else {
          state.finishFailed = true;
          state.finishBlocked = false;
          state.session.status = "incomplete";
          setFinishStatus("结束失败：" + ((err && (err.code || err.message)) || "未知错误") + "（会话未完成，复盘不可用）");
          setError("结束失败：" + ((err && (err.code || err.message)) || "未知错误") + "（会话未完成，Review 不可用）");
        }
        persist(); render();
        return null;
      });
    }
    if (state.replay || state.session._offline || !state.v1 || !state.v1.isLive) {
      state.session.status = "ended";
      setFinishStatus("训练已结束（离线演示）");
      persist(); render();
      return Promise.resolve();
    }
    var v1 = state.v1.client;
    // Finish Barrier：409 表示存在未解决 pending → 引导用户先去 Pending 视图
    return v1.finish().then(function (r) {
      state.session.status = "finished";
      state.finishBlocked = false;
      state.finishFailed = false;
      setFinishStatus("训练已结束");
      persist(); render();
      return r;
    }).catch(function (err) {
      if (err && err.status === 409) {
        state.finishBlocked = true;
        state.finishFailed = false;
        setFinishStatus("结束被阻止：存在未确认的待确认项，请先到「待确认」页处理");
        setError("Finish Barrier：存在未确认的 Pending 项，请先到 Pending 视图处理");
      } else {
        // 未完成（非 barrier）：不显示正式 Review，只显示"未完成"状态
        state.finishFailed = true;
        state.finishBlocked = false;
        state.session.status = "incomplete";
        setFinishStatus("结束失败：" + (err && err.message) + "（会话未完成，复盘不可用）");
        setError("结束失败：" + (err && err.message) + "（会话未完成，Review 不可用）");
        persist();
      }
      render();
      return null;
    });
  }

  function setFinishStatus(text) {
    var el = $("finish-status");
    if (el) el.textContent = text;
  }

  // real：draining → 轮询 GET finish 至 complete（服务端自动闭合）
  function pollFinish(v1, fin, tries) {
    tries = tries || 0;
    if (tries > 60) {
      state.finishFailed = true;
      setFinishStatus("结束处理超时（draining 未闭合）");
      setError("Finish 轮询超时（draining 未闭合）");
      persist(); render();
      return Promise.resolve(null);
    }
    return v1.getFinish().then(function (r) {
      state.finishState = r.data.state;
      if (r.data.complete) {
        state.session.status = "finished";
        state.finishBlocked = false; state.finishFailed = false;
        setFinishStatus("训练已结束（服务端确认完成）");
        return v1.getReview().then(function (rr) { state.review = rr.data; })
          .catch(function () {})
          .then(function () { persist(); render(); return r; });
      }
      return sleep(600).then(function () { return pollFinish(v1, fin, tries + 1); });
    }).catch(function (err) {
      if (err && err.status === 404) {
        // FINISH_NOT_REQUESTED（异常路径）：以相同 finish_id 重试幂等请求
        return v1.finish(fin).then(function (r2) {
          if (r2.data.complete) {
            state.session.status = "finished";
            return v1.getReview().then(function (rr) { state.review = rr.data; })
              .catch(function () {})
              .then(function () { persist(); render(); return r2; });
          }
          return sleep(600).then(function () { return pollFinish(v1, fin, tries + 1); });
        });
      }
      state.finishFailed = true;
      setFinishStatus("结束查询失败：" + ((err && (err.code || err.message)) || "未知错误"));
      setError("Finish 查询失败：" + ((err && (err.code || err.message)) || "未知错误"));
      persist(); render();
      return null;
    });
  }

  function resetAll() {
    wipe();
    state.session = null; state.currentEvent = 0; state.error = null;
    state.stats = { http: 0, replay: 0, retries: 0 };
    state.replay = false;
    state.mode = "mock";
    state.finishBlocked = false;
    state.finishFailed = false;
    state.finishState = null;
    state.review = null;
    state.sessionId = "";
    state.eventCursor = 0;
    state.jobSeq = 0;
    state.lastSeq = -1;
    state.finishId = null;
    state.jobStatuses = {};
    state.transcripts = [];
    state.pendingTexts = {};
    state.sourceAssurance = null;
    state.uploads = {};
    state.v1 = null;
    setFinishStatus("尚未结束");
    var rr = $("resolve-result");
    if (rr) { rr.classList.add("hidden"); rr.innerHTML = ""; }
    var up = $("upload-progress");
    if (up) up.classList.add("hidden");
    switchView("start");
    render();
  }

  // ---------------------------------------------------------------- 渲染
  function setError(msg) { state.error = msg; }
  function switchView(v) {
    state.view = v;
    ["start", "active", "facts", "pending", "correction", "review"].forEach(function (x) {
      var el = $("view-" + x);
      if (el) el.classList.toggle("hidden", x !== v);
    });
    document.querySelectorAll("nav button").forEach(function (b) {
      b.classList.toggle("active", b.dataset.view === v);
    });
  }

  function sessionDone() {
    var s = state.session;
    if (!s) return false;
    if (s.status === "ended" || s.status === "finished") return true;
    var seed = state.data.scenes[state.scene];
    return !!(seed && state.currentEvent >= seed.events.length);
  }

  function reviewAvailable(s) {
    // Phase 3 门禁：Finish 成功（status=finished）才显示正式 Review；
    // barrier（pending 未决）或失败/未完成一律显示"未完成"状态。
    s = s || state.session;
    if (!s) return false;
    if (state.finishBlocked || state.finishFailed) return false;
    return s.status === "finished";
  }

  function isRealLive() {
    return !!(state.mode === "real" && state.v1 && state.v1.isLive && !state.replay);
  }

  // 模式徽标文案（REAL/MOCK 显式；测试ASR模式主徽标）——暴露给测试断言
  function modeBadgeText() {
    if (isRealLive()) {
      return "REAL 模式 · 测试ASR模式";
    }
    if (state.mode === "real") {
      return "REAL 模式（未连接）";
    }
    if (effectiveMode() === "real") {
      return "REAL 模式 · 测试ASR模式（未连接）";
    }
    if (state.v1 && state.v1.isLive) {
      return "CONTRACT_SIM";
    }
    if (state.replay || (state.session && state.session._offline)) {
      return "OFFLINE 回放模式";
    }
    return "MOCK ONLY";
  }

  function render() {
    var s = state.session;
    if (!state.data || !state.data.scenes) {
      setError("演示剧本数据不可用（构建产物损坏？）");
      state.data = state.data || { scenes: {} };
    }
    var seed = state.data && state.data.scenes[state.scene];
    var badge;
    if (isRealLive()) {
      var trust = state.sourceAssurance || "";
      badge = uiBadge("REAL 模式 · 测试ASR模式") +
        " 经受控代理 /realproxy（8814）转发真实 Core（8790）· Token 由代理持有 · 服务端来源信任：" +
        esc(trust || "未标注") + "（测试 Provider 条件，非真实 AI 结果）";
    } else if (state.mode === "real") {
      badge = uiBadge("REAL 模式（未连接）") + " 真实后端未就绪；不自动回退演示数据";
    } else if (state.v1 && state.v1.isLive) {
      badge = uiBadge("CONTRACT_SIM") + " /v1 契约模式（Job 轮询 · Event Cursor · Finish Barrier）";
    } else if (state.replay || (s && s._offline)) {
      badge = uiBadge("OFFLINE 回放模式") + " 未连接 /v1 服务（演示数据同源，无网络请求）";
    } else {
      badge = uiBadge("MOCK ONLY") + " 已连接本地 Mock 后端（X-Mock-Only 响应）";
    }
    var mb = $("mode-banner");
    if (mb) mb.innerHTML = badge;
    var hmb = $("home-mode-badge");
    if (hmb) {
      hmb.innerHTML = uiBadge(modeBadgeText()) +
        (isRealLive()
          ? " 识别结果由后端测试 Provider 返回（确定性测试 ASR，非真实模型）"
          : "（" + (effectiveMode() === "real" ? "测试ASR模式" : "未连接真实后端") + "：开始训练后连接真实后端）");
    }
    var rb = $("retry-banner");
    if (rb) {
      rb.classList.toggle("hidden", !state.error);
      rb.innerHTML = '<button class="btn btn-ghost" onclick="FitcoreDemo.retry()">重试</button> <span>' +
        esc(state.error || "") + "</span>";
    }

    renderStart(s);
    if (!s) {
      // 注意：home-audio-list 由 renderStart→renderHomeAudio 填充，不能在此清空
      ["set-queue", "pending-list", "facts-table", "correction-list", "review-empty", "job-list",
       "pending-summary", "session-card", "facts-session", "preset-audio"].forEach(function (id) {
        var el = $(id);
        if (el) el.innerHTML = "";
      });
      var fh = $("facts-empty");
      if (fh) fh.textContent = "尚无会话：请先在首页开始训练。";
      var rg = $("review-gate");
      if (rg) rg.classList.remove("hidden");
      var rbv = $("review-body");
      if (rbv) rbv.classList.add("hidden");
      return;
    }
    renderActive(s);
    renderFacts(s);
    renderPending(s);
    renderCorrection(s);
    renderReview(s);
  }

  function renderHomeAudio() {
    var el = $("home-audio-list");
    if (!el) return;
    var files = state.data.realWavFiles || {};
    var names = Object.keys(files);
    if (!names.length) {
      el.innerHTML = '<div class="empty">预置音频清单未注入（构建产物缺少 realWavFiles）</div>';
      return;
    }
    el.innerHTML = names.map(function (id) {
      var meta = files[id];
      return '<div class="row"><b>' + esc(meta.label || id) + "</b> · " +
        esc(meta.name || id) + " · " + esc(meta.transcript || "（无固定转录）") +
        '<span class="tag">预置</span></div>';
    }).join("");
  }

  function renderStart(s) {
    var seed = state.data.scenes[state.scene];
    var sel = $("scene-select");
    if (sel) sel.value = state.scene;
    var st = $("start-status");
    if (st) {
      st.innerHTML = s
        ? "当前会话 <b>" + esc(s.id) + "</b> · 状态 <b>" + esc(s.status) + "</b>" +
          (isRealLive() && state.sessionId ? " · session_id <b>" + esc(state.sessionId) + "</b>" : "")
        : "尚未创建会话";
    }
    var sa = $("start-actions");
    if (sa) {
      sa.innerHTML = s
        ? '<button class="btn btn-primary" onclick="FitcoreDemo.toActive()">继续训练</button> ' +
          '<button class="btn btn-ghost" onclick="FitcoreDemo.reset()">重新开始</button>'
        : '<button class="btn btn-primary" onclick="FitcoreDemo.begin()">开始训练</button>';
    }
    var sh = $("start-hint");
    if (sh) {
      sh.innerHTML = s ? "" :
        (requestedMode() === "real"
          ? "Real 模式：合成测试 WAV（正弦音，非真人录音）经真实 Core 4797485 处理；确定性测试 Provider 条件将在页面显著标注。"
          : "创建 Session 后显示录音状态；语音为模拟作业（MOCK，不采集真实麦克风）。");
    }
    var sl = $("scene-list");
    if (sl) {
      sl.innerHTML = seed ? seed.events.map(function (e, i) {
        return "<li><span class='ev-id'>" + esc(e.id) + "</span> " +
          (e.t === "sess" ? "会话事件" : e.t === "end" ? "结束事件" : "「" + esc(e.text) + "」") +
          "</li>";
      }).join("") : "";
    }
    renderHomeAudio();
  }

  function renderSessionCard() {
    var el = $("session-card");
    if (!el) return;
    if (!isRealLive() || !state.session) {
      el.innerHTML = "未连接真实会话";
      return;
    }
    var s = state.session;
    el.innerHTML =
      "会话 <b>" + esc(state.sessionId || s.id) + "</b>" +
      " · 状态 <b>" + esc(s.status) + "</b>" +
      (s.version != null ? " · 版本 v" + esc(s.version) : "") +
      (s.active_exercise_name ? " · 当前动作 <b>" + esc(s.active_exercise_name) + "</b>" : "") +
      (s.last_reported_weight != null ? " · 最近重量 " + esc(s.last_reported_weight) + "kg" : "") +
      (state.sourceAssurance ? " · 来源信任 " + esc(state.sourceAssurance) : "") +
      "（测试ASR模式：识别由测试 Provider 返回）";
  }

  function renderPresets() {
    var el = $("preset-audio");
    if (!el) return;
    var files = state.data.realWavFiles || {};
    var names = Object.keys(files);
    if (!names.length) {
      el.innerHTML = '<div class="empty">预置音频清单未注入</div>';
      return;
    }
    el.innerHTML = names.map(function (id) {
      var meta = files[id];
      return '<div class="row preset-row">' +
        "<div><b>" + esc(meta.label || id) + "</b> · " + esc(meta.name || id) +
        (meta.transcript ? " · 预期「" + esc(meta.transcript) + "」" : "") + "</div>" +
        '<div class="btnrow"><button class="btn btn-primary" data-preset="' + esc(id) + '">提交这条语音</button></div>' +
        "</div>";
    }).join("");
    el.querySelectorAll("button[data-preset]").forEach(function (b) {
      b.addEventListener("click", function () {
        var id = b.dataset.preset;
        FitcoreDemo.submitPreset(id);
      });
    });
  }

  function renderJobList() {
    var el = $("job-list");
    if (!el) return;
    var jobs = Object.keys(state.jobStatuses);
    var jhtml = [];
    jobs.forEach(function (uid) {
      var j = state.jobStatuses[uid];
      var up = state.uploads[uid] || {};
      var st = j.status || "?";
      jhtml.push('<div class="row"><b>' + esc(uid) + "</b>" +
        (j.sequence != null ? " seq " + esc(j.sequence) : "") +
        (up.name ? " · " + esc(up.name) : "") +
        ' → <span class="tag tag-job-' + esc(st) + '">' + esc(st) + " " + esc(jobLabel(st)) + "</span>" +
        (st !== "done" && st !== "blocked" ? ' <span class="muted">（202 已受理≠识别成功，轮询中）</span>' : "") +
        (j.final_text ? " · 转录「" + esc(j.final_text) + "」" : "") +
        (j.asr_attempts != null ? " · 识别尝试 " + esc(j.asr_attempts) : "") +
        (j.error_type ? " · " + esc(j.error_type) : "") + "</div>");
    });
    if (!jhtml.length) {
      jhtml.push('<div class="empty">尚未提交音频作业。8 态：queued 排队中 → asr_running 语音识别中 → final_ready 结果就绪 → llm_running 语义理解中 → <b>done 识别成功</b> / retry_asr、retry_llm 重试 / blocked 失败。</div>');
    }
    el.innerHTML = jhtml.join("");
  }

  function renderActive(s) {
    var seed = state.data.scenes[state.scene];
    var next = seed ? seed.events[state.currentEvent] : null;
    renderSessionCard();
    renderPresets();
    if (isRealLive()) {
      // ---- real：Facts 为训练组唯一权威（GET facts）；Job 只作进度展示 ----
      var q = [];
      (s.facts || []).forEach(function (f) {
        q.push(
          '<div class="row">' +
          "<b>" + esc(f.exercise) + "</b> 第" + esc(f.set_number) + "组 " + esc(f.weight_kg) + "kg × " + esc(f.reps) +
          " 次 · 来源 " + esc(f.weight_provenance === "INHERITED" ? "继承" : "明示") +
          (f.source_utterance_id ? " · 语音 " + esc(f.source_utterance_id) : "") +
          '<span class="tag tag-confirmed">权威事实</span></div>');
      });
      if (!q.length) q.push('<div class="empty">暂无训练事实（GET facts 为空；FACT_COMMITTED 事件不计数）。上传语音识别成功后自动出现。</div>');
      $("set-queue").innerHTML = q.join("");

      renderJobList();

      var lastT = state.transcripts.length ? state.transcripts[state.transcripts.length - 1] : null;
      $("record-status").innerHTML = lastT
        ? "事件 seq " + esc(lastT.seq) + " · " +
          (lastT.text ? "转录「" + esc(lastT.text) + "」" : esc(lastT.type || "")) +
          (state.sourceAssurance ? " · 来源信任 " + esc(state.sourceAssurance) : "")
        : "等待提交语音（测试ASR模式：确定性测试 Provider，合成 WAV → 固定转录）";
    } else {
      var q0 = [];
      s.facts.forEach(function (f) {
        if (f.status === "hidden") return;
        q0.push(
          '<div class="row' + (f.status === "pending" ? " row-pending" : "") + (f.corrected ? " row-corrected" : "") + '">' +
          "<b>" + esc(f.movement) + "</b> " + esc(f.weight_kg) + "kg × " + esc(f.reps) +
          " 次" + (f.approx ? " ⚠近似" : "") + (f.cross_statement ? " ·跨语句" : "") +
          (f.corrected ? " ·已纠错" : "") +
          '<span class="tag tag-' + f.status + '">' + (f.status === "confirmed" ? "已确认" : "待确认") + "</span></div>");
      });
      if (!q0.length) q0.push('<div class="empty">暂无训练事实（等待模拟语音作业）</div>');
      $("set-queue").innerHTML = q0.join("");
      $("job-list").innerHTML = "";

      var last = s.jobs && s.jobs.length ? s.jobs[s.jobs.length - 1] : null;
      $("record-status").innerHTML = last
        ? "模拟作业 " + esc(last.job_id) + " → " + (last.status === "processed" ? "已处理" : esc(last.status)) +
          "（MOCK，非真实 ASR）"
        : "模拟录音中…（MOCK，不采集真实麦克风）";
    }
    var ps = $("pending-summary");
    if (ps) {
      var open = (s.pending || []).filter(function (p) { return p.status === "open"; });
      ps.textContent = open.length
        ? "有 " + open.length + " 项待确认（补充重量与次数后确认入账，或忽略）。"
        : "暂无待确认项。";
    }
    var btn = $("next-btn");
    if (btn) {
      if (sessionDone() || s.status === "ended" || s.status === "finished") {
        btn.textContent = "训练结束";
        btn.onclick = function () { FitcoreDemo.end(); };
      } else {
        btn.textContent = next ? (isRealLive() ? "提交演示语音（" + esc(next.id) + "）" : "模拟语音作业（" + esc(next.id) + "）") : "提交演示语音";
        btn.onclick = function () { FitcoreDemo.next(); };
      }
    }
    var eb = $("end-btn");
    if (eb) {
      eb.disabled = !isRealLive();
      eb.title = isRealLive() ? "" : "未连接真实会话";
    }
    $("progress").textContent = (s.status === "ended" || s.status === "finished")
      ? "已完成全部演示事件"
      : "已演示 " + state.currentEvent + " / " + (seed ? seed.events.length : 0) + " 事件" +
        (isRealLive() ? " · 已提交 seq " + state.jobSeq + " · last_sequence " + state.lastSeq : "");
  }

  // 页面三：训练记录（只来自 Core Facts 响应，零写死）
  function renderFacts(s) {
    var fs = $("facts-session");
    if (fs) {
      fs.innerHTML = isRealLive()
        ? "会话 <b>" + esc(state.sessionId || s.id) + "</b> · 状态 <b>" + esc(s.status) + "</b> · 数据源：GET /v1/sessions/{sid}/facts"
        : (s._offline ? "离线回放（非真实后端）" : "CONTRACT_SIM（非真实后端）");
    }
    var tb = $("facts-table");
    if (!tb) return;
    if (!isRealLive()) {
      // 非 real：显示本地映射事实（表头不变，语义标注）
      var rows0 = (s.facts || []).map(function (f) {
        return "<tr><td>" + esc(f.movement || f.exercise) + "</td><td>" + esc(f.weight_kg) + "</td><td>" + esc(f.reps) +
          "</td><td>" + esc(f.set_number != null ? f.set_number : "-") + "</td><td>" +
          esc(f.weight_provenance === "INHERITED" ? "继承" : "明示") + "</td><td>" + esc(f.source_utterance_id || "-") + "</td></tr>";
      }).join("");
      tb.innerHTML = rows0;
      var fh0 = $("facts-empty");
      if (fh0) fh0.textContent = rows0 ? "" : "暂无训练事实。";
      return;
    }
    var rows = (s.facts || []).map(function (f) {
      return "<tr><td>" + esc(f.exercise) + "</td><td>" + esc(f.weight_kg) + "</td><td>" + esc(f.reps) +
        "</td><td>" + esc(f.set_number) + "</td><td>" +
        esc(f.weight_provenance === "INHERITED" ? "继承" : "明示") + "</td><td>" + esc(f.source_utterance_id || "-") + "</td></tr>";
    }).join("");
    if (!rows) {
      var fh = $("facts-empty");
      if (fh) {
        fh.textContent = "暂无训练事实（后端 Facts 响应为空；上传语音识别成功且入账后出现）。";
      }
      return;
    }
    tb.innerHTML = rows;
    var fh = $("facts-empty");
    if (fh) {
      fh.textContent = rows ? "" : "暂无训练事实（后端 Facts 响应为空；上传语音识别成功且入账后出现）。";
    }
  }

  function renderPending(s) {
    if (window.__JUDGE_TRACE__) {
      window.__JUDGE_TRACE__.push({
        t: "renderPending", view: state.view, mode: state.mode,
        isLive: !!(state.v1 && state.v1.isLive),
        open: (s.pending || []).filter(function (p) { return p.status === "open"; }).map(function (p) { return p.id; }),
        all: (s.pending || []).map(function (p) { return p.id + ":" + p.status; }),
      });
    }
    var rr = $("resolve-result");
    if (rr && state.view !== "pending") rr.classList.add("hidden");
    if (isRealLive()) {
      var open = (s.pending || []).filter(function (p) { return p.status === "open"; });
      var html = [];
      if (!open.length) {
        html.push('<div class="empty">没有待确认项（pending_operations 为空；BARE_NUMBER 等 DEFER 语音会在此出现）</div>');
      }
      open.forEach(function (p) {
        var txt = state.pendingTexts[p.utterance_id];
        var w0 = s.last_reported_weight != null ? s.last_reported_weight : "";
        var r0 = txt ? parseInt(txt, 10) : "";
        html.push(
          '<div class="row" data-pid="' + esc(p.id) + '">' +
          "<b>Pending #" + esc(p.id) + "</b> · reason <b>" + esc(p.reason) + "</b>" +
          (txt ? " · 语音「" + esc(txt) + "」" : " · utterance " + esc(p.utterance_id)) +
          (p.context_version != null ? " · 会话版本 v" + esc(p.context_version) : "") +
          '<div class="btnrow">' +
          '<label>重量kg <input type="number" id="pend-weight-' + esc(p.id) + '" value="' + esc(w0) + '" class="inp" min="0"></label> ' +
          '<label>次数 <input type="number" id="pend-reps-' + esc(p.id) + '" value="' + esc(r0) + '" class="inp" min="0"></label> ' +
          '<button class="btn btn-primary" data-rc="confirm_set" data-pid="' + esc(p.id) + '">确认入账</button> ' +
          '<button class="btn btn-ghost" data-rc="dismiss" data-pid="' + esc(p.id) + '">忽略</button>' +
          "</div></div>");
      });
      var pl = $("pending-list");
      if (pl) pl.innerHTML = html.join("");
      var peh = $("pending-empty-hint");
      if (peh) {
        peh.textContent =
          "正式接口：POST /v1/sessions/{sid}/pending/{pid}/resolve {resolution_id, action: dismiss|confirm_set, context_version, weight_kg, reps}；" +
          "confirm_set 需要重量+次数（否则 409 FULL_SET_CONFIRMATION_REQUIRED）；冲突(409)会显示后端原因。";
      }
      var btns = pl ? pl.querySelectorAll("button[data-rc]") : [];
      btns.forEach(function (b) {
        b.addEventListener("click", function () {
          FitcoreDemo.pending(b.dataset.pid, b.dataset.rc);
        });
      });
      return;
    }
    var open0 = s.pending.filter(function (p) { return p.status === "open"; });
    var html0 = [];
    if (!open0.length) {
      html0.push('<div class="empty">没有待确认项（可演示"大约做了8个"→ 待确认事实，或"还能再做两个"→ RIR 提示）</div>');
    }
    open0.forEach(function (p) {
      if (p.kind === "rir") {
        // L-02 第3条：RIR 只可 dismiss（无"接受"按钮），不增加已完成次数
        html0.push('<div class="row"><b>RIR 余力提示</b>：还能再做 <b>' + esc(p.value) +
          '</b> 个 —— 不增加已完成次数。<div class="btnrow">' +
          '<button class="btn btn-primary" onclick="FitcoreDemo.pending(\'' + p.id + '\',\'dismiss\')">忽略提示</button></div></div>');
      }
    });
    // L-02：pending 列表由 pending 通道驱动（fact 类项按 fact_id 关联 facts 明细）
    var pendingFacts = s.pending.filter(function (p) { return p.kind === "fact"; });
    pendingFacts.forEach(function (p) {
      var f = s.facts.find(function (x) { return x.id === p.fact_id; }) || {};
      html0.push('<div class="row"><b>' + esc(f.movement || "?") + "</b> " + esc(f.weight_kg || "?") + "kg × " +
        esc(f.reps || "?") + " 次" + (f.approx ? " ⚠近似" : "") + (f.cross_statement ? " ·跨语句" : "") +
        '<div class="btnrow"><button class="btn btn-primary" onclick="FitcoreDemo.pending(\'' + p.id +
        '\',\'confirm_set\')">确认入账</button> <button class="btn btn-ghost" onclick="FitcoreDemo.pending(\'' + p.id +
        '\',\'dismiss\')">忽略</button></div></div>');
    });
    $("pending-list").innerHTML = html0.join("");
    $("pending-empty-hint").textContent = "规则提示：近似次数需人工确认后方可入账（MOCK 展示规则，非 Track A 入账逻辑）。";
  }

  function renderCorrection(s) {
    var hint = $("correction-hint");
    var list = $("correction-list");
    if (isRealLive()) {
      // 真实 Core 无 facts/{fid}/correction 路由：明确标注"未接线"，不伪造纠错
      var cand = (s.facts || []);
      var html = [];
      if (!cand.length) {
        html.push('<div class="empty">暂无训练组（先回“训练进行中”提交语音）</div>');
      }
      cand.forEach(function (f) {
        html.push(
          '<div class="row" data-fact="' + esc(f.id) + '">' +
          "<b>" + esc(f.exercise) + "</b> 第" + esc(f.set_number) + "组 " + esc(f.weight_kg) + "kg × " + esc(f.reps) +
          " 次 · 来源 " + esc(f.weight_provenance === "INHERITED" ? "继承" : "明示") +
          "</div>");
      });
      if (list) list.innerHTML = html.join("");
      if (hint) {
        hint.textContent =
          "暂不可用：真实 Core 4797485 未接线事实纠错路由（14 条正式路由无 facts/{fid}/correction）。" +
          "本视图只读展示；纠错语义请用 CORRECTION 语音（如“刚才第一组其实是50公斤”）经 Fact Engine 承担。";
      }
      return;
    }
    var cand0 = s.facts.filter(function (f) { return f.status !== "hidden"; });
    var html0 = [];
    if (!cand0.length) html0.push('<div class="empty">暂无训练组可纠错（先回“训练进行中”生成训练组）</div>');
    cand0.forEach(function (f) {
      html0.push(
        '<div class="row" data-fact="' + esc(f.id) + '">' +
        "<b>" + esc(f.movement) + "</b> 当前：<span class='cur'>" + esc(f.weight_kg) + "kg × " + esc(f.reps) +
        " 次</span>" + (f.approx ? " ⚠近似" : "") + (f.corrected ? " ·已纠错" : "") +
        '<div class="btnrow"><label>次数 <input type="number" min="0" value="' + esc(f.reps) +
        '" class="inp inp-reps" data-id="' + esc(f.id) + '"></label> ' +
        '<button class="btn btn-primary" onclick="FitcoreDemo.correct(\'' + f.id + '\',\'reps\')">修改次数</button></div>' +
        "</div>");
    });
    if (list) list.innerHTML = html0.join("");
    if (hint) {
      hint.textContent =
        "纠错只修改现有训练组，不新增组。演示：对 F-0001（8 次）改 7 → 组数不变。";
    }
  }

  function reviewSummary(s) {
    s = s || state.session;
    if (!s) return { sets: 0, reps: 0, approx: 0, corrected: 0, open: 0 };
    var c = (s.facts || []).filter(function (f) { return f.status === "confirmed"; });
    return {
      sets: c.length,
      reps: c.reduce(function (a, f) { return a + f.reps; }, 0),
      approx: c.filter(function (f) { return f.approx; }).length,
      corrected: c.filter(function (f) { return f.corrected; }).length,
      open: (s.pending || []).filter(function (p) { return p.status === "open"; }).length,
    };
  }

  function renderFinishStatus(s) {
    var el = $("finish-status");
    if (!el) return;
    if (!s) { el.textContent = "尚无会话"; return; }
    if (s.status === "finished") { el.textContent = "训练已结束（服务端确认完成）。"; return; }
    if (s.status === "draining") { el.textContent = "结束处理中：等待剩余语音作业完成后服务端自动闭合。"; return; }
    if (state.finishBlocked) { el.textContent = "结束被阻止（409 awaiting_audio）：请等待语音作业完成或补传缺失序列后重试。"; return; }
    if (state.finishFailed) { el.textContent = "结束失败（会话未完成），复盘不可用。"; return; }
    el.textContent = "尚未结束：点击「结束训练」提交 Finish 请求。";
  }

  function renderReview(s) {
    var ok = reviewAvailable();
    var rg = $("review-gate");
    if (rg) rg.classList.toggle("hidden", ok);
    var rb = $("review-body");
    if (rb) rb.classList.toggle("hidden", !ok);
    renderFinishStatus(s);
    if (!ok) {
      var why;
      if (!s) {
        why = "尚无会话。";
      } else if (state.finishBlocked) {
        why = "训练尚未完成：Finish Barrier（409 awaiting_audio：存在未完成/缺失序列的音频作业）。请补传缺失序列后以相同 finish_id 重试。";
      } else if (state.finishFailed || s.status === "incomplete") {
        why = "训练结束失败（会话未完成）：不显示正式 Review。请回到训练视图重试结束。";
      } else if (s.status === "draining") {
        why = "Finish 屏障已进入 draining：等待全部音频作业完成（服务端自动闭合），稍后刷新。";
      } else {
        why = "训练尚未完成：Review 只展示已完成（finish 成功）会话的已确认事实。请先完成演示事件并点击“结束训练”。";
      }
      $("review-empty").innerHTML = '<div class="empty">' + esc(why) + "</div>";
      return;
    }
    if (isRealLive() && state.review) {
      // ---- real：ReviewRead 正式字段映射（数字只来自后端响应）----
      var rev = state.review;
      $("review-stats").innerHTML =
        "<b>总组数</b> " + esc(rev.total_sets) + " · <b>总次数</b> " + esc(rev.total_reps) +
        " · <b>总容量(kg×reps)</b> " + esc(rev.volume_kg_reps) +
        " · <b>训练容量(kg×reps)</b> " + esc(rev.volume_kg_reps) +
        " · <b>未决 pending</b> " + esc(rev.pending_count) +
        " · <b>context_version</b> " + esc(rev.context_version);
      var rows = [];
      if (!(rev.facts || []).length) rows.push('<div class="empty">没有训练组（facts 为空）</div>');
      (rev.facts || []).forEach(function (f) {
        rows.push(
          '<div class="row"><b>' + esc(f.exercise) + "</b> 第" + esc(f.set_number) + "组 " +
          esc(f.weight_kg) + "kg × " + esc(f.reps) + " 次 · 来源 " +
          esc(f.weight_provenance === "INHERITED" ? "继承" : "明示") +
          " · fact_id " + esc(f.id) + "</div>");
      });
      $("review-list").innerHTML = rows.join("");
      $("review-meta").innerHTML =
        "数据来源：GET /v1/sessions/{sid}/review（ReviewRead 正式字段）；Facts 为完成训练组唯一权威，" +
        "FACT_COMMITTED 事件不计数。测试条件：确定性测试 ASR + AUDIO_TEST_SOURCE_TRUSTED=true（服务端来源信任），" +
        "非真实模型/AI 结果。";
      return;
    }
    var c = s.facts.filter(function (f) { return f.status === "confirmed"; });
    var sm = reviewSummary();
    $("review-stats").innerHTML =
      "<b>已确认训练组</b> " + sm.sets + " 组 · <b>总次数</b> " + sm.reps + " 次 · " +
      "<b>近似标记</b> " + sm.approx + " · <b>已纠错</b> " + sm.corrected + " · " +
      "<b>未处理待确认</b> " + sm.open;
    var rows0 = [];
    if (!c.length) rows0.push('<div class="empty">没有已确认的训练组（只统计已确认事实）</div>');
    c.forEach(function (f) {
      rows0.push('<div class="row' + (f.corrected ? " row-corrected" : "") + '">' +
        "<b>" + esc(f.movement) + "</b> " + esc(f.weight_kg) + "kg × " + esc(f.reps) + " 次" +
        (f.approx ? " ⚠近似" : "") + (f.corrected ? " ·已纠错" : "") + "</div>");
    });
    $("review-list").innerHTML = rows0.join("");
    var ignored = s.facts.filter(function (f) { return f.status === "hidden"; }).length;
    $("review-meta").innerHTML =
      "RIR 提示不计入次数；忽略的训练组 " + ignored + " 组不展示、不计入统计。" +
      "<br>数据来源：GET facts（已确认事实）聚合；Job 状态不推导组数。";
  }

  // ---------------------------------------------------------------- 文件上传
  function uploadFile() {
    var input = $("wav-file");
    if (!input || !input.files || !input.files.length) {
      setError("请先选择一个 WAV 文件（16kHz / 单声道 / PCM16，≤7MB）");
      render();
      return;
    }
    var file = input.files[0];
    if (!/\.wav$/i.test(file.name)) {
      setError("只支持 .wav 文件（" + file.name + "）");
      render();
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      var buf = new Uint8Array(reader.result);
      var info = readWavInfo(buf);
      if (!info.ok) {
        setError("WAV 预检不通过（" + file.name + "）：" + info.reason +
                 "。服务端 422 仍是最终裁决（静音/DC 由后端判定）。");
        render();
        return;
      }
      if (buf.length > CFG.wav.maxBytes) {
        setError("WAV 超过最大 " + CFG.wav.maxBytes + " 字节（实际 " + buf.length + "）");
        render();
        return;
      }
      var uid = "U-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 6);
      state.uploads[uid] = { name: file.name, bytes: buf.length };
      input.value = "";
      submitAudio(buf, file.name, uid)
        .then(function () { render(); })
        .catch(function () { render(); });
    };
    reader.onerror = function () { setError("读取文件失败（" + file.name + "）"); render(); };
    reader.readAsArrayBuffer(file);
  }

  function submitPreset(id) {
    if (!isRealLive()) {
      setError("未连接真实会话：请先在首页点击「开始训练」");
      render();
      return;
    }
    var b64 = state.data.realWavB64 && state.data.realWavB64[id];
    var meta = (state.data.realWavFiles || {})[id] || {};
    if (!b64) {
      setError("预置音频缺失（fixture 未注入）：" + id);
      render();
      return;
    }
    var bytes = Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); });
    var uid = meta.uid || ("U-" + id);
    var name = meta.name || (id + ".wav");
    submitAudio(bytes, name, uid)
      .then(function () { render(); })
      .catch(function () { render(); });
  }

  // 对外：训练记录页主动刷新（只来自 Core Facts）
  window.FitcoreDemoInternals.refreshFactsNow = function () {
    return refreshReal().then(function () { render(); });
  };

  // ---------------------------------------------------------------- 对外
  window.FitcoreDemo = {
    begin: function () { switchView("active"); startSession(state.scene).then(function () { switchView("active"); }); },
    next: function () {
      if (sessionDone()) return this.end();
      nextEvent().then(refreshAll);
    },
    toActive: function () { switchView("active"); render(); },
    fact: function (fid, d) { resolveFact(fid, d); },
    correct: function (fid, field) {
      var el = document.querySelector('.inp-reps[data-id="' + fid + '"]');
      correctFact(fid, field, el ? el.value : 0);
    },
    pending: function (pid, d, w, r) { resolvePending(pid, d, w, r); },
    uploadFile: uploadFile,
    submitPreset: submitPreset,
    refreshFacts: function () { refreshReal().then(function () { render(); }); },
    end: function () { endSession().then(refreshAll); },
    retry: function () { state.error = null; refreshAll(); },
    reset: function () { resetAll(); },
    view: function (v) { switchView(v); render(); },
    sceneChanged: function (el) { state.scene = el.value; renderStart(state.session); },
  };

  // 初始化
  function init() {
    var el = document.getElementById("fitcore-demo-data");
    if (el) {
      try { state.data = JSON.parse(el.textContent); }
      catch (e) { state.data = null; setError("演示剧本数据解析失败"); }
    }
    var restored = restore();
    if (!restored) {
      var sc = $("scene-select");
      if (sc && sc.value) state.scene = sc.value;
    } else if (restored && state.mode === "real" && state.sessionId) {
      // 页面重载恢复：重建 real 客户端并按持久化的 session_id 全量恢复
      var v1 = makeV1("real");
      if (v1) {
        v1.setSessionId(state.sessionId);
        state.v1 = { client: v1, isLive: true };
        refreshAll();
      }
    }
    render();
  }
  document.addEventListener("DOMContentLoaded", init);
})();
