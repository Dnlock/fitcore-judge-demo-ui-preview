/* Fitcore /v1 真实契约客户端（纯逻辑模块，无 DOM 依赖）。
 *
 * 设计目标：真实后端接入时，前端传输层只替换本模块的实现细节；
 * 演示页 HTML/CSS 与视图逻辑不变。
 *
 * 安全边界：
 *  - 不在浏览器源码中内置任何 Bearer Token；
 *  - 真实远程 API 必须经受控服务端代理（见 app.js CFG.proxy）；
 *  - 当前默认目标为本地 CONTRACT_SIM（http://127.0.0.1:8812/v1），
 *    或离线回放（offline()），绝不伪造真实联调结果。
 *
 * 契约（来自现场执行令第五节，SHA 9bef66f 描述；L-02 修正见矩阵）：
 *  POST /v1/sessions
 *  POST /v1/sessions/{sid}/audio-jobs       (202 = 仅持久化接收)
 *  GET  /v1/sessions/{sid}/audio-jobs/{uid} (轮询：L-02 正式态 queued→asr_running→final_ready→llm_running→done/failed；
 *                                            当前 ws-demo v1_sim 兼容旧态 submitted→processing→processed/failed)
 *  GET  /v1/sessions/{sid}/events?cursor=N  (Event Cursor 断线恢复)
 *  GET  /v1/sessions/{sid}/facts
 *  GET  /v1/sessions/{sid}/pending
 *  POST /v1/sessions/{sid}/pending/{pid}/resolve
 *  POST /v1/sessions/{sid}/finish           (409 = Finish Barrier)
 *  GET  /v1/sessions/{sid}/finish
 *  GET  /v1/sessions/{sid}/review           (409 = 未 finish)
 */
var FitcoreV1Client = (function () {
  "use strict";

  function V1Client(opts) {
    opts = opts || {};
    this.base = opts.base || "http://127.0.0.1:8812/v1";
    this.sessionId = opts.sessionId || "S-0001";
    this.fetchImpl = opts.fetchImpl ||
      (typeof fetch !== "undefined"
        ? (typeof window !== "undefined" ? fetch.bind(window) : fetch)
        : null);
    this.log = opts.log || function () {};
    this.pendingFacts = []; // 会话未完成，暂不暴露事实确认（真实契约只有 pending resolve）
  }

  V1Client.prototype._call = function (method, path, body) {
    var self = this;
    var url = this.base + path;
    var opts = { method: method, headers: { "Content-Type": "application/json" } };
    if (body) opts.body = JSON.stringify(body);
    var p;
    if (this.fetchImpl) {
      p = this.fetchImpl(url, opts);
    } else {
      p = new Promise(function (resolve, reject) {
        var xhr = new XMLHttpRequest();
        xhr.open(method, url, true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onreadystatechange = function () {
          if (xhr.readyState !== 4) return;
          var data;
          try { data = JSON.parse(xhr.responseText); } catch (e) { data = { raw: xhr.responseText }; }
          if (xhr.status >= 200 && xhr.status < 300) resolve({ status: xhr.status, data: data, headers: {} });
          else reject(Object.assign(new Error(data && data.error ? data.error.message : ("HTTP " + xhr.status)), { status: xhr.status, data: data }));
        };
        xhr.onerror = function () { reject(new Error("网络不可达")); };
        xhr.send(body ? JSON.stringify(body) : null);
      });
    }
    return p.then(function (res) {
      var status = res.status, data = res.data;
      if (status >= 200 && status < 300) return { status: status, data: data };
      throw Object.assign(new Error(data && data.error ? data.error.message : ("HTTP " + status)), { status: status, data: data });
    });
  };

  // ---- 契约路由 ----
  V1Client.prototype.createSession = function (meta) {
    return this._call("POST", "/sessions", meta ? { meta: meta } : {});
  };
  V1Client.prototype.submitAudioJob = function (audioB64, uid) {
    var body = { audio: audioB64 };
    if (uid) body.job_uid = uid;
    return this._call("POST", "/sessions/" + this.sessionId + "/audio-jobs", body);
  };
  V1Client.prototype.getJob = function (uid) {
    return this._call("GET", "/sessions/" + this.sessionId + "/audio-jobs/" + uid);
  };
  V1Client.prototype.getEvents = function (cursor) {
    var q = cursor ? "?cursor=" + cursor : "";
    return this._call("GET", "/sessions/" + this.sessionId + "/events" + q);
  };
  V1Client.prototype.getFacts = function () {
    return this._call("GET", "/sessions/" + this.sessionId + "/facts");
  };
  V1Client.prototype.getPending = function () {
    return this._call("GET", "/sessions/" + this.sessionId + "/pending");
  };
  V1Client.prototype.resolvePending = function (pid, decision) {
    return this._call("POST", "/sessions/" + this.sessionId + "/pending/" + pid + "/resolve", { decision: decision });
  };
  V1Client.prototype.finish = function () {
    return this._call("POST", "/sessions/" + this.sessionId + "/finish");
  };
  V1Client.prototype.getFinish = function () {
    return this._call("GET", "/sessions/" + this.sessionId + "/finish");
  };
  V1Client.prototype.getReview = function () {
    return this._call("GET", "/sessions/" + this.sessionId + "/review");
  };

  // ---- Job 轮询 ----
  // 终态集合：L-02 正式 done/failed；兼容旧态 processed（ws-demo v1_sim 状态机迁移 WAITING）
  V1Client.TERMINAL_STATES = ["done", "failed", "processed"];
  V1Client.prototype.pollJob = function (uid, onState, intervalMs, maxTries) {
    var self = this;
    intervalMs = intervalMs || 400;
    maxTries = maxTries || 25;
    var tries = 0;
    function tick() {
      if (tries++ >= maxTries) {
        var e = new Error("作业轮询超时");
        e.timeout = true;
        throw e;
      }
      return self.getJob(uid).then(function (r) {
        var j = r.data;
        onState(j.status, j);
        if (V1Client.TERMINAL_STATES.indexOf(j.status) >= 0) return j;
        return new Promise(function (res) { setTimeout(res, intervalMs); }).then(tick);
      });
    }
    return Promise.resolve().then(tick);
  };

  // ---- Event Cursor 断线恢复 ----
  V1Client.prototype.drainEvents = function (onEvent, cursor) {
    var self = this;
    var c = cursor || 0;
    function step() {
      return self.getEvents(c).then(function (r) {
        var evs = r.data.events || [];
        evs.forEach(onEvent);
        if (r.data.next_cursor == null) return r.data.next_cursor;
        c = r.data.next_cursor;
        if (!evs.length) return c;
        return step();
      });
    }
    return step();
  };

  // ---- 离线回放（演示降级；数据同源，非真实联调） ----
  V1Client.offline = function (scenes, scene) {
    var s = { id: "S-0001", status: "recording", facts: [], pending: [],
              corrections: [], events: [], jobs: [], _offline: true };
    function replay(ev) {
      if (s.events.some(function (x) { return x.id === ev.id; })) return;
      s.events.push(ev);
      (ev.facts || []).forEach(function (f) {
        var dup = s.facts.some(function (x) {
          return x.movement === f[0] && x.weight_kg === f[1] && x.reps === f[2] && !!x.approx === !!f[3].approx;
        });
        if (dup) return;
        s.facts.push({ id: "F-" + String(s.facts.length + 1).padStart(4, "0"),
                       movement: f[0], weight_kg: f[1], reps: f[2],
                       status: f[3].explicit ? "confirmed" : "pending",
                       approx: !!f[3].approx, cross_statement: !!f[3].cross_statement,
                       corrected: false, correction_ids: [], dup_events: [] });
      });
      if (ev.rir != null) {
        s.pending.push({ id: "P-" + String(s.pending.length + 1).padStart(4, "0"),
                         kind: "rir", value: ev.rir, status: "open" });
      }
      if (s.status === "recording") s.status = "active";
    }
    var seed = scenes.scenes[scene];
    if (seed.sess) replay(seed.sess);
    seed.events.forEach(replay);
    return { session: s, scene: scene };
  };

  return V1Client;
})();

// Node 环境导出（pytest 经 node 调用本文件做纯逻辑测试）
if (typeof module !== "undefined" && module.exports) {
  module.exports = FitcoreV1Client;
}
