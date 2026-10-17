"""Short SQLite barrier transactions; never hold the audio/network SessionLock."""
import json

from pydantic import Field

from app import service
from app.schemas import StrictModel


class FinishIn(StrictModel):
    finish_id: str = Field(min_length=1, max_length=120)
    last_sequence: int = Field(ge=-1, le=100000, strict=True)


class FinishConflict(ValueError):
    pass


def jobs(db, sid):
    return [dict(r) for r in db.execute('SELECT utterance_id,sequence,audio_sha256,status,error_type '
                                       'FROM audio_jobs WHERE session_id=? ORDER BY sequence', (sid,))]


def missing_sequences(entries, last):
    present = {r['sequence'] for r in entries}
    return [seq for seq in range(last + 1) if seq not in present]


def _view(db, row):
    entries = jobs(db, row['session_id'])
    missing = missing_sequences(entries, row['last_sequence'])
    unfinished = [r for r in entries if r['status'] != 'done']
    result = {'session_id': row['session_id'], 'finish_id': row['finish_id'],
              'last_sequence': row['last_sequence'], 'state': row['state'],
              'complete': row['state'] == 'finished', 'acceptance_closed': row['state'] != 'awaiting_audio',
              'missing_sequences': missing[:1000], 'missing_count': len(missing),
              'unfinished': unfinished, 'accepted_count': len(entries),
              'ready_to_finalize': row['state'] == 'draining' and not missing and not unfinished,
              'review': json.loads(row['review_json']) if row['review_json'] else None}
    return result


def status(db, sid):
    owns = not db.in_transaction
    if owns:
        db.execute('BEGIN')
    try:
        row = db.execute('SELECT * FROM session_finishes WHERE session_id=?', (sid,)).fetchone()
        return _view(db, row) if row else None
    finally:
        if owns:
            db.rollback()


def request(db, sid, body: FinishIn):
    db.execute('BEGIN IMMEDIATE')
    try:
        ctx = service.context(db, sid)
        if ctx is None:
            raise FinishConflict('SESSION_NOT_FOUND')
        row = db.execute('SELECT * FROM session_finishes WHERE session_id=?', (sid,)).fetchone()
        if row:
            if row['finish_id'] != body.finish_id or row['last_sequence'] != body.last_sequence:
                raise FinishConflict('FINISH_ID_OR_RANGE_CONFLICT')
        else:
            if ctx.status != 'active':
                raise FinishConflict('SESSION_ALREADY_ENDED')
            entries = jobs(db, sid)
            if entries and entries[-1]['sequence'] > body.last_sequence:
                raise FinishConflict('FINISH_RANGE_EXCLUDES_ACCEPTED_AUDIO')
            db.execute("INSERT INTO session_finishes(session_id,finish_id,last_sequence,state) VALUES (?,?,?,'awaiting_audio')",
                       (sid, body.finish_id, body.last_sequence))
        row = db.execute('SELECT * FROM session_finishes WHERE session_id=?', (sid,)).fetchone()
        if row['state'] == 'awaiting_audio':
            entries = jobs(db, sid)
            # An incomplete request is NOT an accepted Finish: repair missing input then
            # retry the same ID/range. Never close and silently drop an in-transit tail.
            if not missing_sequences(entries, body.last_sequence):
                manifest = [{k: r[k] for k in ('utterance_id', 'sequence', 'audio_sha256')} for r in entries]
                db.execute("UPDATE session_finishes SET state='draining',manifest_json=?,closed_at=CURRENT_TIMESTAMP "
                           'WHERE session_id=?', (json.dumps(manifest), sid))
        row = db.execute('SELECT * FROM session_finishes WHERE session_id=?', (sid,)).fetchone()
        if row['state'] == 'draining':
            entries = jobs(db, sid)
            manifest = [{k: r[k] for k in ('utterance_id', 'sequence', 'audio_sha256')} for r in entries]
            if manifest != json.loads(row['manifest_json']):
                raise FinishConflict('ACCEPTED_AUDIO_MANIFEST_CHANGED')
            if all(r['status'] == 'done' for r in entries):
                # Administrative closure after ALL audio acknowledgements, without a new LLM call.
                db.execute("UPDATE sessions SET status='ended',version=version+1,ended_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'", (sid,))
                review = service.review(db, sid).model_dump_json()
                db.execute("UPDATE session_finishes SET state='finished',review_json=?,finished_at=CURRENT_TIMESTAMP WHERE session_id=?", (review, sid))
        result = _view(db, db.execute('SELECT * FROM session_finishes WHERE session_id=?', (sid,)).fetchone())
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def check_enqueue(db, sid, sequence):
    # Must be called in the SAME transaction as INSERT audio_jobs.
    row = db.execute('SELECT state,last_sequence FROM session_finishes WHERE session_id=?', (sid,)).fetchone()
    if row:
        if row['state'] != 'awaiting_audio':
            raise FinishConflict('AUDIO_ACCEPTANCE_CLOSED')
        if sequence > row['last_sequence']:
            raise FinishConflict('AUDIO_BEYOND_REQUESTED_FINISH_RANGE')


def review_blocker(db, sid):
    barrier = status(db, sid)
    if barrier and not barrier['complete']:
        return barrier
    if barrier is None and db.execute('SELECT 1 FROM audio_jobs WHERE session_id=? LIMIT 1', (sid,)).fetchone():
        return {'state': 'finish_required', 'complete': False, 'reason': 'FINISH_REQUIRED'}
    return None
