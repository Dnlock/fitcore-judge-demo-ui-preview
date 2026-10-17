#!/usr/bin/env python3
"""ws-judge-core · 28 步 E2E 冒烟（复刻 WS-E DEMO_HTTP_E2E 断言）+ 公网门禁探针。

入口：对外代理（8956/80）——浏览器/客户端只与代理通信；隔离 Core（8957/8001）
只经代理转发、仅供直连探针验证 401/403 拒绝面。
测试条件声明：FAKE_TEST 确定性 ASR（sha256→固定转录）+ MODEL_PROVIDER=fake
（上游内置确定性语义）——测试ASR模式，非真实模型、零网络、零付费调用。

用法:
  python3 e2e/smoke_e2e.py --proxy http://127.0.0.1:8956 --core http://127.0.0.1:8957 \
      --wav-dir fixtures/wav
退出码: 0 = 28/28 全 OK。
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

APP_KEY = "fitcore-demo-app-v1"
APP_KEY_HEADER = "X-Demo-App-Key"


def call(base, method, path, body=None, headers=None, raw=None, expect=None):
    data = json.dumps(body).encode() if body is not None else raw
    req = urllib.request.Request(base + path, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read())
            hdrs = dict(r.headers)
            if expect is not None:
                assert r.status == expect, f"{method} {path}: {r.status} != {expect}: {payload}"
            return r.status, payload, hdrs
    except urllib.error.HTTPError as e:
        rawb = e.read()
        try:
            payload = json.loads(rawb)
        except Exception:
            payload = {"raw": rawb.decode("utf-8", "replace")[:200]}
        if expect is not None:
            assert e.code == expect, f"{method} {path}: {e.code} != {expect}: {payload}"
        return e.code, payload, dict(e.headers)


def multipart_upload(base, sid, uid, seq, wav_path, extra_fields=None, audio_source="client_complete_wav"):
    boundary = "----ws-jc-" + uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

    field("utterance_id", uid)
    field("sequence", str(seq))
    field("audio_source", audio_source)
    for k, v in (extra_fields or {}).items():
        field(k, v)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="complete.wav"\r\n'
        f'Content-Type: audio/wav\r\n\r\n'.encode())
    parts.append(wav_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return call(base, "POST", f"/sessions/{sid}/audio-jobs", raw=b"".join(parts),
                headers={APP_KEY_HEADER: APP_KEY,
                         "Content-Type": f"multipart/form-data; boundary={boundary}"})


def poll_job(base, sid, uid, timeout=30):
    deadline = time.time() + timeout
    states = []
    while time.time() < deadline:
        st, jr, _ = call(base, "GET", f"/sessions/{sid}/audio-jobs/{uid}",
                         headers={APP_KEY_HEADER: APP_KEY}, expect=200)
        job = jr.get("job", {})
        if job.get("status") not in states:
            states.append(job.get("status"))
        if job.get("status") in ("done", "blocked"):
            return job, states
        time.sleep(0.1)
    raise AssertionError(f"poll timeout: {states}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:8956")
    ap.add_argument("--core", default="http://127.0.0.1:8957")
    ap.add_argument("--wav-dir", default=str(Path(__file__).resolve().parents[1] / "fixtures" / "wav"))
    args = ap.parse_args()
    base = args.proxy.rstrip("/") + "/realproxy/v1"
    wav = Path(args.wav_dir)
    steps, ok = [], True

    def check(name, cond, detail=""):
        nonlocal ok
        steps.append(name)
        tag = "OK " if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{tag}] {name} {detail}")

    # 0. 代理健康
    st, h, _ = call(args.proxy, "GET", "/health")
    check("proxy_health", st == 200 and h.get("token_configured") is True, f"upstream={h.get('upstream')}")

    # 1. 会话创建 → 服务端实际 UUID
    st, s, _ = call(base, "POST", "/sessions", {}, {APP_KEY_HEADER: APP_KEY, "Content-Type": "application/json"}, expect=201)
    sid = s["session_id"]
    check("session_uuid_201", st == 201 and len(sid) >= 8 and s["status"] == "active", f"sid={sid}")

    # 2. multipart 上传（sha256 → FAKE_TEST 固定转录「卧推60公斤8个」）
    st, up, hdrs = multipart_upload(base, sid, "U-0001", 0, wav / "real_E-0002.wav")
    check("upload_multipart_202", st == 202 and up.get("acceptance") == "persisted",
          f"assurance={up.get('source_assurance')} mode={hdrs.get('X-Fitcore-Mode')}")
    check("upload_source_assurance", up.get("source_assurance") == "server_authorized_test",
          "（服务端测试信任条件：AUDIO_TEST_SOURCE_TRUSTED=true，界面标注测试ASR模式）")

    # 3. Job 8 态轮询 → done + 转录
    job, states = poll_job(base, sid, "U-0001")
    check("job_done_8state", job["status"] == "done" and job.get("final_text") == "卧推60公斤8个",
          f"states={states} asr_attempts={job.get('asr_attempts')}")

    # 4. Facts 唯一权威
    st, facts, _ = call(base, "GET", f"/sessions/{sid}/facts", headers={APP_KEY_HEADER: APP_KEY}, expect=200)
    check("facts_authoritative", len(facts) == 1 and facts[0]["weight_kg"] == 60.0 and facts[0]["reps"] == 8
          and facts[0]["weight_provenance"] == "EXPLICIT", f"facts={facts}")

    # 5. Events after/limit 分页
    st, evs, _ = call(base, "GET", f"/sessions/{sid}/events?after=0&limit=1",
                      headers={APP_KEY_HEADER: APP_KEY}, expect=200)
    check("events_after_limit", evs["events"][0]["type"] == "TRANSCRIPT_FINAL" and evs["has_more"] is True
          and evs["next_cursor"] == 1, f"first={evs['events'][0]['event_seq']} next={evs['next_cursor']}")
    st, evs2, _ = call(base, "GET", f"/sessions/{sid}/events?after={evs['next_cursor']}&limit=100",
                       headers={APP_KEY_HEADER: APP_KEY}, expect=200)
    check("events_incremental", evs2["events"][0]["type"] == "FACT_COMMITTED" and evs2["has_more"] is False,
          f"next={evs2['next_cursor']}")
    st, ahead, _ = call(base, "GET", f"/sessions/{sid}/events?after=9999&limit=10",
                        headers={APP_KEY_HEADER: APP_KEY}, expect=409)
    check("events_cursor_ahead_409", ahead.get("detail") == "CURSOR_AHEAD_OF_SESSION", f"detail={ahead.get('detail')}")

    # 6. BARE_NUMBER 语音 → pending
    st, up2, _ = multipart_upload(base, sid, "U-0002", 1, wav / "real_E-0003.wav")
    check("upload_bare_number", st == 202, "seq=1")
    job2, _ = poll_job(base, sid, "U-0002")
    check("bare_number_job_done", job2["status"] == "done", f"final_text={job2.get('final_text')!r}")
    st, pend, _ = call(base, "GET", f"/sessions/{sid}/pending", headers={APP_KEY_HEADER: APP_KEY}, expect=200)
    check("pending_bare_number", len(pend) == 1 and pend[0]["reason"] == "BARE_NUMBER"
          and pend[0]["status"] == "open", f"pending={pend}")

    # 7. Finish 三态：缺序列 → 409 awaiting_audio；补传后同 finish_id 幂等重试 → 200
    st, fin409, _ = call(base, "POST", f"/sessions/{sid}/finish",
                         {"finish_id": "F-1", "last_sequence": 2},
                         {APP_KEY_HEADER: APP_KEY, "Content-Type": "application/json"}, expect=409)
    check("finish_awaiting_audio_409", fin409.get("state") == "awaiting_audio"
          and 2 in fin409.get("missing_sequences", []), f"missing={fin409.get('missing_sequences')}")
    st, up3, _ = multipart_upload(base, sid, "U-0003", 2, wav / "real_E-0005.wav")
    poll_job(base, sid, "U-0003")
    st, fin200, _ = call(base, "POST", f"/sessions/{sid}/finish",
                         {"finish_id": "F-1", "last_sequence": 2},
                         {APP_KEY_HEADER: APP_KEY, "Content-Type": "application/json"}, expect=200)
    check("finish_retry_idempotent_200", fin200.get("complete") is True and fin200.get("state") == "finished",
          f"accepted={fin200.get('accepted_count')}")
    st, finGet, _ = call(base, "GET", f"/sessions/{sid}/finish", headers={APP_KEY_HEADER: APP_KEY}, expect=200)
    check("get_finish_200", finGet.get("complete") is True, f"state={finGet.get('state')}")

    # 8. Review 正式字段
    st, rev, _ = call(base, "GET", f"/sessions/{sid}/review", headers={APP_KEY_HEADER: APP_KEY}, expect=200)
    check("review_formal_fields", rev["total_sets"] == 1 and rev["total_reps"] == 8
          and rev["volume_kg_reps"] == 480.0 and rev["pending_count"] == 1
          and len(rev["facts"]) == 1,
          f"review={ {k: rev[k] for k in ('total_sets','total_reps','volume_kg_reps','pending_count')} }")

    # 9. pending resolve → RESOLVED；重复 → DUPLICATE
    st, ctx, _ = call(base, "GET", f"/sessions/{sid}", headers={APP_KEY_HEADER: APP_KEY}, expect=200)
    pid = pend[0]["id"]
    req = {"resolution_id": "R-1", "action": "confirm_set", "context_version": ctx["version"],
           "weight_kg": 60, "reps": 8}
    st, res, _ = call(base, "POST", f"/sessions/{sid}/pending/{pid}/resolve", req,
                      {APP_KEY_HEADER: APP_KEY, "Content-Type": "application/json"}, expect=200)
    check("resolve_confirm_set", res["decision"] == "RESOLVED" and len(res["committed"]) == 1,
          f"committed={res['committed'][0]['type']} v{res['context_version']}")
    st, facts2, _ = call(base, "GET", f"/sessions/{sid}/facts", headers={APP_KEY_HEADER: APP_KEY}, expect=200)
    check("facts_after_resolve", len(facts2) == 2, f"facts={len(facts2)}")
    st, dup, _ = call(base, "POST", f"/sessions/{sid}/pending/{pid}/resolve", req,
                      {APP_KEY_HEADER: APP_KEY, "Content-Type": "application/json"}, expect=200)
    check("resolve_duplicate_idempotent", dup["decision"] == "DUPLICATE", f"decision={dup['decision']}")

    # 10. 上传幂等：同 uid 同内容 → 已有 job，组数不变
    st, upIdem, _ = multipart_upload(base, sid, "U-0001", 0, wav / "real_E-0002.wav")
    st2f, facts3, _ = call(base, "GET", f"/sessions/{sid}/facts", headers={APP_KEY_HEADER: APP_KEY}, expect=200)
    check("upload_idempotent_same_uid", st == 202 and upIdem.get("job", {}).get("utterance_id") == "U-0001"
          and len(facts3) == 2, f"facts_unchanged={len(facts3)}")

    # 11. 错误信封与安全
    st, e404, _ = call(base, "GET", f"/sessions/{sid}/audio-jobs/U-NOPE",
                       headers={APP_KEY_HEADER: APP_KEY}, expect=404)
    check("error_detail_404", e404.get("detail") == "AUDIO_JOB_NOT_FOUND", f"detail={e404.get('detail')}")
    silent = wav / "f6_finish.wav"
    st, eSil, _ = multipart_upload(base, sid, "U-SILENT", 3, silent)
    check("silent_wav_422", st == 422 and eSil.get("detail") == "INVALID_COMPLETE_WAV", f"detail={eSil.get('detail')}")
    st, eDupF, _ = multipart_upload(base, sid, "U-EXTRA", 4, wav / "real_E-0002.wav", extra_fields={"hack": "1"})
    check("extra_form_field_422", st == 422
          and eDupF.get("detail") == "UNSUPPORTED_OR_DUPLICATE_METADATA_SERVER_ASSIGNS_SOURCE_TRUST",
          f"detail={eDupF.get('detail')}")
    st, eSess, _ = multipart_upload(base, "no-such-sid", "U-X", 0, wav / "real_E-0002.wav")
    check("session_not_found_404", st == 404 and eSess.get("detail") == "SESSION_NOT_FOUND",
          f"detail={eSess.get('detail')}")
    st, eKey, _ = call(base, "GET", f"/sessions/{sid}", headers={}, expect=403)
    check("app_key_required_403", eKey.get("error", {}).get("code") == "app_key_required")
    # 直连 Core 缺 Bearer → 401（代理替服务端注入，浏览器永不带 Token）
    st, eAuth, _ = call(args.core, "GET", "/v1/sessions/" + sid, headers={}, expect=401)
    check("auth_required_401", eAuth.get("detail") == "AUTHENTICATION_REQUIRED")

    # 12. 静态页面同源伺服 + 模式标识
    try:
        req0 = urllib.request.Request(args.proxy + "/")
        with urllib.request.urlopen(req0, timeout=15) as r0:
            page = r0.read()
            hdrs = dict(r0.headers)
        st = 200
    except Exception:
        st, page, hdrs = 0, b"", {}
    check("static_page_real_mode", st == 200 and hdrs.get("X-Fitcore-Mode") == "real"
          and b"fitcore-demo-data" in page, f"x-mode={hdrs.get('X-Fitcore-Mode')} bytes={len(page)}")
    st, healthReal, _ = call(args.proxy, "GET", "/health")
    check("proxy_health_final", st == 200 and healthReal.get("token_configured") is True)

    print(f"\nE2E_RESULT: {'ALL_OK' if ok else 'HAS_FAILURES'} {len(steps)} steps")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
