/* Fitcore /v1 Real HTTP Adapter（Phase 2 起 · Phase 4 真实契约修正版）
 *
 * 面向真实 Core 4797485 的 /v1 HTTP 契约实现（依据 REAL_CONTRACT_VERIFY.md 12 条差异）：
 *  - 传输模式 mode: "mock"（CONTRACT_SIM 兼容，保留 Phase2 旧契约行为）| "real"（真实契约）
 *  - real 契约要点：
 *      · POST /v1/sessions        body 可空 → 201 ContextRead{ session_id: <UUID|自定义>,
 *                                   version, status, active_exercise_instance_id, ... }
 *      · POST /v1/sessions/{sid}/audio-jobs  multipart/form-data：
 *          file(完整WAV) + utterance_id(1..120) + sequence(≥0) + audio_source(默认 client_complete_wav)
 *          → 202 {acceptance, speaker_verification, source_assurance, job}
 *      · GET /v1/sessions/{sid}/audio-jobs/{uid} → {speaker_verification, automatic_retry_exhausted, job}
 *          job.status ∈ queued|asr_running|final_ready|llm_running|retry_asr|retry_llm|blocked|done
 *      · GET /v1/sessions/{sid}/events?after=N&limit=M（≤100）→ {events, next_cursor, has_more}
 *      · GET /v1/sessions/{sid}/facts → [FactRead]（完成训练组唯一权威）
 *      · GET /v1/sessions/{sid}/pending → [PendingRead]
 *      · POST /v1/sessions/{sid}/pending/{pid}/resolve
 *          body {resolution_id, action: dismiss|confirm_set, context_version, weight_kg?, reps?}
 *          → PendingResolutionOut {pending_id, resolution_id, decision: RESOLVED|DISMISSED|DUPLICATE,
 *                                  context_version, committed}
 *      · POST /v1/sessions/{sid}/finish body {finish_id, last_sequence}
 *          → 200 complete=true / 202 draining / 409 awaiting_audio；GET finish 未请求过 → 404
 *      · GET /v1/sessions/{sid}/review → ReviewRead {session_id, context_version, total_sets,
 *          total_reps, volume_kg_reps, pending_count, facts}
 *      · 错误信封：{"detail": "<大写下划线错误码>"}（FastAPI HTTPException）
 *  - mock 模式保持 Phase2/Phase3 旧契约（JSON base64 上传、cursor 事件、{decision} resolve、
 *    finish 无请求体），保证既有 61 项测试与 v1_sim 演示不回归。
 *
 * 安全边界（与 Phase3 一致）：
 *  - 浏览器源码不含任何 Bearer Token；real 模式只带内置 App 密钥头 X-Demo-App-Key
 *    与会话 Cookie 调同源受控代理（server/real_proxy.py）；真实 Token 只在代理服务端。
 *  - real 模式失败不静默降级（不把演示数据冒充真实数据）。
 *
 * 可靠性：
 *  - 超时/重试：网络错误/超时/408/425/429/5xx 线性退避重试；4xx 业务错误不重试；
 *  - 幂等：同一 (method,path) 的并发/重复请求共享同一 in-flight Promise（防止双击重复提交）；
 *    上传幂等由服务端 (session_id, utterance_id) 主键保证（同 ID 同内容 → 返回已有 job）。
 *
 * 用法：
 *   var a = new FitcoreHTTPAdapter({ mode: "real", base: "/realproxy/v1" });
 *   a.createSession().then(r => a.sessionId = r.data.session_id);
 *   a.submitAudioJob(b64, { uid: "U-0001", sequence: 0 })   // real: multipart
 *     .then(...pollJob("U-0001", onState)...)
 *     .then(...drainEvents(onEvent, 0)...)
 *     .then(...resolvePending(pid, {resolution_id, action:"confirm_set", context_version, weight_kg, reps})...)
 *     .then(...finish({finish_id: "F-1", last_sequence: N})...)
 *     .then(...getReview()...);
 */
var FitcoreHTTPAdapter = (function () {
  "use strict";

  var VERSION = "2.2.0";
  var DEFAULT_BASE = "http://127.0.0.1:8812/v1";           // CONTRACT_SIM 开发端口（mock 模式）
  var DEFAULT_REAL_BASE = "/realproxy/v1";                  // 同源受控代理（real 模式）
  // 内置 App 密钥：只证明"请求来自本演示应用"，不是后端凭据；
  // 真实后端 Bearer Token 由 real_proxy 服务端持有，浏览器永不见。
  var APP_KEY = "fitcore-demo-app-v1";
  var APP_KEY_HEADER = "X-Demo-App-Key";
  var DEFAULT_TIMEOUT_MS = 8000;
  var DEFAULT_RETRIES = 2;
  var DEFAULT_RETRY_DELAY_MS = 200;
  // 可安全重试的 HTTP 状态；网络错误与超时一律重试。4xx 业务错误（含 409 屏障）不重试。
  var RETRYABLE_STATUS = { 408: true, 425: true, 429: true, 500: true, 502: true, 503: true, 504: true };
  // 真实 Core 8 态 Job 状态机（db.py CHECK 枚举）；终态 = done（成功）/ blocked（最终失败）。
  // failed / processed 为 CONTRACT_SIM 旧状态机终态，mock 模式兼容保留。
  var REAL_JOB_TERMINAL = ["done", "blocked"];
  var LEGACY_JOB_TERMINAL = ["done", "failed", "processed"];

  function HttpAdapterError(message, status, code, data, retryable) {
    var e = new Error(message || ("HTTP " + (status || "网络错误")));
    e.name = "HttpAdapterError";
    e.status = status || 0;
    e.code = code || "http_error";
    e.data = data || null;
    e.retryable = !!retryable;
    return e;
  }

  // 跨 realm 稳健判断（vm/iframe/worker 中 instanceof 会失效，按 name 判断）
  function isAdapterError(e) {
    return !!(e && e.name === "HttpAdapterError");
  }

  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  // 真实契约错误信封：{"detail":"CODE"}；兼容模拟器 {"error":{"code","message"}}
  function extractError(status, data) {
    if (data && typeof data.detail === "string" && data.detail) {
      return { code: data.detail, message: data.detail };
    }
    var ep = (data && data.error) || {};
    return { code: ep.code || ("http_" + status), message: ep.message };
  }

  function HTTPAdapter(opts) {
    opts = opts || {};
    this.mode = opts.mode === "real" ? "real" : "mock";
    this.base = opts.base ||
      (this.mode === "real" ? DEFAULT_REAL_BASE : DEFAULT_BASE);
    this.sessionId = opts.sessionId || "";    // real：创建会话后从响应取真实 session_id
    this.timeoutMs = typeof opts.timeoutMs === "number" ? opts.timeoutMs : DEFAULT_TIMEOUT_MS;
    this.retries = typeof opts.retries === "number" ? opts.retries : DEFAULT_RETRIES;
    this.retryDelayMs = typeof opts.retryDelayMs === "number" ? opts.retryDelayMs : DEFAULT_RETRY_DELAY_MS;
    this.fetchImpl = opts.fetchImpl ||
      (typeof fetch !== "undefined"
        ? (typeof window !== "undefined" ? fetch.bind(window) : fetch)
        : null);
    this.log = opts.log || function () {};
    this.listeners = {};
    this.stats = { requests: 0, retries: 0, errors: 0 };
    this.inflight = {}; // 幂等：相同 (method,path) 的重复请求共享同一 Promise
    this.terminalJobStates = (this.mode === "real" ? REAL_JOB_TERMINAL : LEGACY_JOB_TERMINAL).slice();
    if (opts.terminalJobStates) this.terminalJobStates = opts.terminalJobStates.slice();
  }

  HTTPAdapter.prototype.setSessionId = function (sid) {
    this.sessionId = sid;
    return this;
  };

  // ------------------------------------------------------------ 事件接口
  HTTPAdapter.prototype.on = function (evt, fn) {
    if (!this.listeners[evt]) this.listeners[evt] = [];
    this.listeners[evt].push(fn);
    return this;
  };
  HTTPAdapter.prototype.off = function (evt, fn) {
    var ls = this.listeners[evt] || [];
    var i = ls.indexOf(fn);
    if (i >= 0) ls.splice(i, 1);
    return this;
  };
  HTTPAdapter.prototype._emit = function (evt, payload) {
    (this.listeners[evt] || []).forEach(function (fn) {
      try { fn(payload); } catch (e) { /* 回调异常不影响主流程 */ }
    });
  };

  function isFormData(body) {
    return typeof FormData !== "undefined" && body instanceof FormData;
  }

  // ------------------------------------------------------------ 传输实现
  HTTPAdapter.prototype._fetch = function (method, url, body, timeoutMs) {
    var controller = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var timer = null;
    if (controller) {
      timer = setTimeout(function () { controller.abort(); }, timeoutMs);
    }
    var opts = {
      method: method,
      headers: {},
      // 同源请求：携带会话 Cookie（代理自身鉴权/审计用；与后端 Token 无关）
      credentials: "same-origin",
    };
    if (this.mode === "real") {
      // Real 模式：内置 App 密钥头（应用身份，非后端凭据）；绝不带 Authorization
      opts.headers[APP_KEY_HEADER] = APP_KEY;
    }
    if (body !== undefined && body !== null) {
      if (isFormData(body)) {
        // multipart/form-data：由浏览器自动设置 boundary，绝不手动指定 Content-Type
        opts.body = body;
      } else {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
      }
    }
    if (controller) opts.signal = controller.signal;
    var self = this;
    return this.fetchImpl(url, opts).then(function (res) {
      if (timer) clearTimeout(timer);
      return res.text().then(function (text) {
        var data;
        try { data = text ? JSON.parse(text) : {}; }
        catch (e) { data = { raw: text }; }
        var headers = {};
        if (res.headers && typeof res.headers.forEach === "function") {
          res.headers.forEach(function (v, k) { headers[k] = v; });
        }
        return { status: res.status, data: data, headers: headers };
      });
    }).catch(function (err) {
      if (timer) clearTimeout(timer);
      if (err && err.name === "AbortError") {
        var e = new Error("请求超时（" + timeoutMs + "ms）");
        e.code = "timeout";
        throw e;
      }
      throw err;
    });
  };

  HTTPAdapter.prototype._xhr = function (method, url, body, timeoutMs) {
    var self = this;
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open(method, url, true);
      if (self.mode === "real") {
        xhr.setRequestHeader(APP_KEY_HEADER, APP_KEY);
      }
      if (body !== undefined && body !== null && !isFormData(body)) {
        xhr.setRequestHeader("Content-Type", "application/json");
      }
      xhr.timeout = timeoutMs;
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        var text = xhr.responseText || "";
        var data;
        try { data = text ? JSON.parse(text) : {}; }
        catch (e) { data = { raw: text }; }
        resolve({ status: xhr.status, data: data, headers: {} });
      };
      xhr.ontimeout = function () {
        var e = new Error("请求超时（" + timeoutMs + "ms）");
        e.code = "timeout";
        reject(e);
      };
      xhr.onerror = function () { reject(new Error("网络错误")); };
      var payload = null;
      if (body !== undefined && body !== null) {
        payload = isFormData(body) ? body : JSON.stringify(body);
      }
      xhr.send(payload);
    });
  };

  HTTPAdapter.prototype._request = function (method, path, body, opts) {
    var self = this;
    opts = opts || {};
    var retries = typeof opts.retries === "number" ? opts.retries : this.retries;
    var timeoutMs = typeof opts.timeoutMs === "number" ? opts.timeoutMs : this.timeoutMs;
    var url = this.base + path;
    var key = method + " " + path;
    var attempt = 0;

    // 幂等保护：同一 (method,path) 已有在途请求时复用（防双击/重入重复提交）
    if (this.inflight[key]) return this.inflight[key];

    function once() {
      self.stats.requests++;
      var send = self.fetchImpl ? self._fetch : self._xhr;
      return send.call(self, method, url, body, timeoutMs)
        .then(function (res) {
          if (res.status >= 200 && res.status < 300) {
            self._emit("response", { method: method, path: path, status: res.status });
            return { status: res.status, data: res.data, headers: res.headers };
          }
          var ep = extractError(res.status, res.data);
          var retryable = !!(RETRYABLE_STATUS[res.status] || res.status >= 500);
          throw new HttpAdapterError(
            ep.message, res.status, ep.code, res.data, retryable);
        })
        .catch(function (err) {
          if (isAdapterError(err)) throw err;
          var code = (err && err.code === "timeout") ? "timeout" : "network_error";
          throw new HttpAdapterError(err && err.message, 0, code, null, true);
        });
    }

    function attemptOnce() {
      return once().catch(function (err) {
        if (!isAdapterError(err) || !err.retryable || attempt >= retries) {
          self.stats.errors++;
          self._emit("error", err);
          throw err;
        }
        attempt++;
        self.stats.retries++;
        var delay = self.retryDelayMs * attempt;
        self.log("httpAdapter retry " + attempt + "/" + retries + " " + method + " " + path + " after " + delay + "ms");
        return sleep(delay).then(attemptOnce);
      });
    }

    var p = attemptOnce().then(function (r) { delete self.inflight[key]; return r; },
                               function (e) { delete self.inflight[key]; throw e; });
    this.inflight[key] = p;
    return p;
  };

  // ------------------------------------------------------------ /v1 契约路由
  HTTPAdapter.prototype.health = function () {
    return this._request("GET", "/health");
  };

  // real：POST /v1/sessions body 可空 → 201；调用方从 data.session_id 取真实 ID。
  // mock：保持 Phase2 旧契约 {meta, reset}。
  HTTPAdapter.prototype.createSession = function (meta) {
    var self = this;
    var body = (this.mode === "real") ? {} : { meta: meta || {} };
    if (meta && meta.reset !== undefined && this.mode !== "real") body.reset = meta.reset;
    return this._request("POST", "/sessions", body)
      .then(function (r) {
        if (self.mode === "real" && r.data && r.data.session_id) {
          self.sessionId = r.data.session_id; // 服务端返回的实际 session_id（UUID）
        }
        self._emit("session", { action: "create", data: r.data });
        return r;
      });
  };

  HTTPAdapter.prototype.getSession = function () {
    return this._request("GET", "/sessions/" + this.sessionId);
  };

  // real：multipart/form-data（file + utterance_id + sequence + audio_source）→ 202。
  // mock：JSON base64 {audio, job_uid}（Phase2 旧契约，v1_sim 兼容）。
  HTTPAdapter.prototype.submitAudioJob = function (audioB64, meta) {
    var self = this;
    if (this.mode !== "real") {
      var body = { audio: audioB64 };
      // 兼容两种调用签名：submitAudioJob(b64, "J-6001") 与 submitAudioJob(b64, {uid, sequence})
      var muid = (meta && typeof meta === "object") ? meta.uid : meta;
      if (muid) body.job_uid = muid;
      return this._request("POST", "/sessions/" + this.sessionId + "/audio-jobs", body)
        .then(function (r) {
          self._emit("job", {
            action: "submit", uid: r.data.uid, status: r.data.status,
            already_applied: !!r.data.already_applied, job: r.data,
          });
          return r;
        });
    }
    // real：multipart 构建（浏览器/Node ≥18 提供 FormData、Blob、atob）
    meta = meta || {};
    var uid = meta.uid || ("U-" + Date.now().toString(36));
    var fd = new FormData();
    var bin = (typeof atob === "function") ? atob(audioB64) : "";
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    fd.append("file", new Blob([bytes], { type: "audio/wav" }), "complete.wav");
    fd.append("utterance_id", String(uid));
    fd.append("sequence", String(meta.sequence == null ? 0 : meta.sequence));
    fd.append("audio_source", String(meta.audioSource || "client_complete_wav"));
    return this._request("POST", "/sessions/" + this.sessionId + "/audio-jobs", fd)
      .then(function (r) {
        var job = r.data.job || {};
        self._emit("job", {
          action: "submit", uid: job.utterance_id || uid, status: job.status,
          acceptance: r.data.acceptance, source_assurance: r.data.source_assurance,
          job: r.data,
        });
        return r;
      });
  };

  HTTPAdapter.prototype.getJob = function (uid) {
    return this._request("GET", "/sessions/" + this.sessionId + "/audio-jobs/" + uid);
  };

  // real：?after=N&limit=M；mock：?cursor=N（Phase2 旧契约）
  HTTPAdapter.prototype.getEvents = function (after, limit) {
    var q;
    if (this.mode === "real") {
      q = "?after=" + (after == null ? 0 : after) + "&limit=" + (limit == null ? 100 : limit);
    } else {
      q = (after != null) ? "?cursor=" + after : "";
    }
    return this._request("GET", "/sessions/" + this.sessionId + "/events" + q);
  };

  HTTPAdapter.prototype.getFacts = function () {
    return this._request("GET", "/sessions/" + this.sessionId + "/facts");
  };

  HTTPAdapter.prototype.getPending = function () {
    return this._request("GET", "/sessions/" + this.sessionId + "/pending");
  };

  // real：完整 resolve 请求体（resolution_id/action/context_version/weight_kg/reps）。
  // mock：{decision}（Phase2 旧契约透传）。
  HTTPAdapter.prototype.resolvePending = function (pid, req) {
    if (this.mode !== "real") {
      // mock：{decision}（Phase2 旧契约透传）；兼容字符串/对象两种调用
      var decision = (typeof req === "string") ? req : (req && (req.action || req.decision));
      return this._request("POST", "/sessions/" + this.sessionId + "/pending/" + pid + "/resolve",
                           { decision: decision });
    }
    var body = {
      resolution_id: String(req.resolution_id || ("R-" + Date.now().toString(36))),
      action: req.action,
      context_version: req.context_version,
    };
    if (req.weight_kg != null) body.weight_kg = req.weight_kg;
    if (req.reps != null) body.reps = req.reps;
    return this._request("POST", "/sessions/" + this.sessionId + "/pending/" + pid + "/resolve", body);
  };

  // Correction：真实 Core 4797485 无事实纠错路由（14 条路由清单无 facts/{fid}/correction），
  // real 模式调用会得到 404 NOT_FOUND（明确"未接线"）；mock 模式走 v1_sim 兼容路由。
  HTTPAdapter.prototype.correctFact = function (fid, changes) {
    return this._request("POST", "/sessions/" + this.sessionId + "/facts/" + fid + "/correction",
                         changes || {});
  };

  HTTPAdapter.prototype.resolveFact = function (fid, decision) {
    return this._request("POST", "/sessions/" + this.sessionId + "/facts/" + fid + "/resolve",
                         { decision: decision });
  };

  // real：Finish 屏障事务（finish_id + last_sequence）；mock：空体（Phase2 旧契约）。
  HTTPAdapter.prototype.finish = function (req) {
    var body = {};
    if (this.mode === "real") {
      req = req || {};
      body.finish_id = String(req.finish_id || ("F-" + Date.now().toString(36)));
      body.last_sequence = req.last_sequence == null ? -1 : req.last_sequence;
    }
    return this._request("POST", "/sessions/" + this.sessionId + "/finish", body);
  };

  HTTPAdapter.prototype.getFinish = function () {
    return this._request("GET", "/sessions/" + this.sessionId + "/finish");
  };

  HTTPAdapter.prototype.getReview = function () {
    return this._request("GET", "/sessions/" + this.sessionId + "/review");
  };

  // ------------------------------------------------------------ Job 轮询
  // real：8 态（queued/asr_running/final_ready/llm_running/retry_asr/retry_llm/blocked/done）
  // mock：Phase2 旧状态机（submitted/processing/processed）。终态集合可配置。
  HTTPAdapter.prototype.pollJob = function (uid, onState, intervalMs, maxTries) {
    var self = this;
    intervalMs = intervalMs || 400;
    maxTries = maxTries || 60;
    var tries = 0;
    var last = null;
    var terminal = this.terminalJobStates;
    function tick() {
      if (tries++ >= maxTries) {
        var e = new HttpAdapterError(
          "作业轮询超时: " + uid + "（最后状态 " + (last || "unknown") + "）",
          0, "poll_timeout", { job_uid: uid, last_status: last }, true);
        self._emit("error", e);
        throw e;
      }
      return self.getJob(uid).then(function (r) {
        // real：{speaker_verification, automatic_retry_exhausted, job:{...}}；mock：job 平铺
        var j = (self.mode === "real") ? r.data.job : r.data;
        last = j.status;
        if (onState) onState(j.status, j);
        self._emit("job", { action: "poll", uid: uid, status: j.status, job: j });
        if (terminal.indexOf(j.status) >= 0) return j;
        return sleep(intervalMs).then(tick);
      });
    }
    return Promise.resolve().then(tick);
  };

  // ------------------------------------------------------------ Events 增量同步
  // real：after/limit 分页（next_cursor=最后一条 event_seq；has_more 继续拉）；
  // mock：cursor 旧契约。断线恢复：按 next_cursor 增量拉取；重复事件去重（幂等回调）。
  HTTPAdapter.prototype.drainEvents = function (onEvent, after) {
    var self = this;
    var c = (after != null) ? after : 0;
    var seen = {};
    function step() {
      return self.getEvents(c).then(function (r) {
        var evs = r.data.events || [];
        evs.forEach(function (ev) {
          var id = (self.mode === "real") ? ev.event_seq : ev.id;
          if (seen[id]) return; // 幂等：重复事件不重复回调
          seen[id] = true;
          if (onEvent) onEvent(ev);
          self._emit("event", ev);
        });
        if (self.mode === "real") {
          if (r.data.has_more) {
            c = r.data.next_cursor;
            return step();
          }
          return r.data.next_cursor; // 返回新 after 游标（无事件时 = 原 after）
        }
        if (r.data.next_cursor == null) return null;
        c = r.data.next_cursor;
        if (!evs.length) return c;
        return step();
      });
    }
    return step();
  };

  // 静态导出
  HTTPAdapter.VERSION = VERSION;
  HTTPAdapter.DEFAULT_BASE = DEFAULT_BASE;
  HTTPAdapter.DEFAULT_REAL_BASE = DEFAULT_REAL_BASE;
  HTTPAdapter.APP_KEY_HEADER = APP_KEY_HEADER;
  HTTPAdapter.TERMINAL_JOB_STATES = LEGACY_JOB_TERMINAL;          // mock 兼容
  HTTPAdapter.REAL_TERMINAL_JOB_STATES = REAL_JOB_TERMINAL;       // real 8 态终态
  HTTPAdapter.HttpAdapterError = HttpAdapterError;

  return HTTPAdapter;
})();

// Node 环境导出（pytest 经 node 调用本文件做纯逻辑/真实 HTTP 测试）
if (typeof module !== "undefined" && module.exports) {
  module.exports = FitcoreHTTPAdapter;
}
