"""SQLite transactional event log with per-session cursors and persistent dedup keys."""
import json


def append(db, sid, key, event_type, uid, payload):
    owns = not db.in_transaction
    if owns:
        db.execute('BEGIN IMMEDIATE')
    try:
        old = db.execute('SELECT event_seq FROM session_events WHERE session_id=? AND event_key=?', (sid, key)).fetchone()
        if old:
            seq = old[0]
        else:
            seq = db.execute('SELECT COALESCE(MAX(event_seq),0)+1 FROM session_events WHERE session_id=?', (sid,)).fetchone()[0]
            db.execute('INSERT INTO session_events(session_id,event_seq,event_key,type,utterance_id,payload_json) '
                       'VALUES (?,?,?,?,?,?)', (sid, seq, key, event_type, uid, json.dumps(payload, ensure_ascii=False)))
        if owns:
            db.commit()
        return seq
    except Exception:
        if owns:
            db.rollback()
        raise


def core_result(db, sid, uid, decision, version, committed, reason=None):
    kind = {'COMMIT': 'FACT_COMMITTED', 'DEFER': 'OPERATION_DEFERRED', 'REJECT': 'INPUT_REJECTED',
            'ERROR': 'PROCESSING_ERROR', 'STALE_CONTEXT': 'PROCESSING_ERROR'}.get(decision)
    if kind is None:
        return
    payload = {'decision': decision, 'context_version': version, 'committed': committed, 'reason': reason}
    if decision == 'COMMIT':
        # The stored event type set is fixed by the SQLite CHECK constraint and
        # the HTTP contract, so non-completion commits stay FACT_COMMITTED but
        # carry an explicit scope: consumers counting completed sets must only
        # count events whose event_scope is 'training_facts'.
        # R7 candidate contract (CANDIDATE_API_DELTA.md): training_facts covers
        # business commits SET_COMPLETED/CORRECTION/TRAINING_NOTE_ADDED.
        # training_facts never means "a new set was created": consumers must
        # still inspect committed[].type and fact ids.
        fact_types = {'SET_COMPLETED', 'CORRECTION', 'TRAINING_NOTE_ADDED'}
        state_types = {'EXERCISE_SWITCH', 'NEXT_SET_PLAN_CHANGED', 'SESSION_END'}
        if any(x['type'] in fact_types for x in committed):
            payload['event_scope'] = 'training_facts'
        elif any(x['type'] in state_types for x in committed):
            payload['event_scope'] = 'state_change'
        else:
            return
    key = f'error:utterance:{uid}:semantic' if kind == 'PROCESSING_ERROR' else f'utterance:{uid}:result'
    append(db, sid, key, kind, uid, payload)


def query(db, sid, after=0, limit=100):
    if after < 0 or not 1 <= limit <= 100:
        raise ValueError('INVALID_CURSOR_QUERY')
    db.execute('BEGIN')
    try:
        maximum = db.execute('SELECT COALESCE(MAX(event_seq),0) FROM session_events WHERE session_id=?', (sid,)).fetchone()[0]
        if after > maximum:
            raise ValueError('CURSOR_AHEAD_OF_SESSION')
        rows = db.execute('SELECT * FROM session_events WHERE session_id=? AND event_seq>? ORDER BY event_seq LIMIT ?',
                          (sid, after, limit + 1)).fetchall()
        items = [{'event_seq': r['event_seq'], 'event_id': f'{sid}:event:{r["event_seq"]}', 'type': r['type'],
                  'utterance_id': r['utterance_id'], 'payload': json.loads(r['payload_json']),
                  'created_at': r['created_at']} for r in rows[:limit]]
        return {'events': items, 'next_cursor': items[-1]['event_seq'] if items else after, 'has_more': len(rows) > limit}
    finally:
        db.rollback()
