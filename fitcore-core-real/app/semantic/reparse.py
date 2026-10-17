"""One durable selective model attempt per existing utterance. No ASR retries."""
import json
import time
from app.schemas import ContextRead, SemanticResult, UtteranceIn, UtteranceOut
from app.semantic.coverage import check_coverage
from app.semantic.grounding import verify_grounding, EXERCISES, WEIGHT_UNIT, SIGNED_WEIGHT
from app.context_safety import REFERENCE


def eligible(reason):
    return reason == 'SEMANTIC_COVERAGE_INCOMPLETE' or bool(reason and reason.startswith('CORRECTION_TARGET_'))


def context_rebind(text, context, parsed):
    """Verifiable context inheritance for a lone, ungrounded completion.

    A single SET_COMPLETED whose exercise/weight are not supported by the text
    (no exercise alias, no unit-bearing weight) but whose utterance carries a
    reference marker ('还是这个重量，11个') must bind to the active context
    instead of a model-guessed older instance: clear the model's exercise and
    weight so the normal inheritance path resolves them against the verified
    context. Any other shape is returned untouched, keeping the conservative
    rules in force everywhere else.
    """
    ops = parsed.operations
    if len(ops) != 1 or parsed.ambiguities:
        return parsed
    op = ops[0]
    if (op.intent != 'SET_COMPLETED' or op.who != 'self' or op.when != 'current' or
            op.modality != 'fact'):
        return parsed
    if not REFERENCE.search(text):
        return parsed
    if context.last_reported_weight is None or op.weight_kg in (None, context.last_reported_weight):
        return parsed
    if WEIGHT_UNIT.search(text) or SIGNED_WEIGHT.search(text):
        return parsed
    if any(alias in text for aliases in EXERCISES.values() for alias in aliases):
        return parsed
    if context.next_set_plan not in (None, context.last_reported_weight):
        return parsed
    rebound = op.model_copy(update={'exercise': None, 'weight_kg': None, 'exercise_instance_id': None})
    return SemanticResult(operations=[rebound], ambiguities=parsed.ambiguities)


def result_reason(text, context, original, parsed):
    reason = verify_grounding(text, context, original, parsed)
    if check_coverage(text, parsed, context):
        reason = 'SEMANTIC_COVERAGE_INCOMPLETE'
    if parsed.ambiguities:
        reason = 'REPARSE_AMBIGUOUS'
    return reason


def recover(db, provider, sid, uid):
    from app import service
    ctx = service.context(db, sid)
    if ctx is None:
        return None
    def output(reason, decision='DEFER', meta=None):
        return UtteranceOut(utterance_id=uid, decision=decision, context_version=service.context(db, sid).version,
                            reason=reason, reparse=meta)
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute('SELECT * FROM utterances WHERE session_id=? AND id=?', (sid, uid)).fetchone()
        attempt = db.execute('SELECT * FROM semantic_reparses WHERE session_id=? AND utterance_id=?', (sid, uid)).fetchone()
        pending = db.execute("SELECT * FROM pending_operations WHERE session_id=? AND utterance_id=? AND status='open'",
                             (sid, uid)).fetchone()
        if attempt:
            db.commit()
            if attempt['outcome_json']:
                value = UtteranceOut.model_validate_json(attempt['outcome_json'])
                return value.model_copy(update={'decision': 'DUPLICATE', 'committed': [],
                    'reparse': {'state': attempt['state'], 'elapsed_ms': attempt['elapsed_ms']}})
            if attempt['state'] != 'ready':
                return output(attempt['reason'] or 'REPARSE_ATTEMPT_ALREADY_CLAIMED',
                              meta={'state': attempt['state'], 'elapsed_ms': attempt['elapsed_ms']})
        else:
            if not row or not pending or not eligible(pending['reason']) or not callable(getattr(provider, 'reparse', None)):
                db.commit()
                return output('REPARSE_NOT_ELIGIBLE')
            ctx = service.context(db, sid)
            if ctx.status != 'active' or pending['context_version_at_defer'] != ctx.version:
                db.commit()
                return output('REPARSE_CONTEXT_CHANGED')
            payload = json.loads(pending['payload_json'])
            original = SemanticResult.model_validate({k:payload[k] for k in ('operations','ambiguities')})
            diagnostics = payload.get('coverage_diagnostics') or [{'code': pending['reason']}]
            db.execute('INSERT INTO semantic_reparses(session_id,utterance_id,pending_id,state,context_json,'
                       'original_json,diagnostics_json) VALUES (?,?,?,?,?,?,?)',
                       (sid, uid, pending['id'], 'in_flight', ctx.model_dump_json(), original.model_dump_json(),
                        json.dumps(diagnostics, ensure_ascii=False)))
            db.commit()  # Claim before network IO; never repeat an uncertain remote call after restart.
            started = time.perf_counter()
            try:
                parsed = provider.reparse(row['transcript'], ctx, original, diagnostics)
                parsed = SemanticResult.model_validate(parsed.model_dump() if isinstance(parsed, SemanticResult) else parsed)
                parsed = context_rebind(row['transcript'], ctx, parsed)
                reason = result_reason(row['transcript'], ctx, original, parsed)
                state = 'blocked' if reason else 'ready'
            except Exception:
                parsed, state, reason = None, 'failed', 'REPARSE_PROVIDER_OR_SCHEMA_ERROR'
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            # Do not persist arbitrary provider error bodies or credential-bearing exceptions.
            safe = {k:v for k,v in getattr(provider, 'last_diagnostics', {}).items()
                    if k in {'http_status', 'elapsed_ms', 'error_type', 'schema_parse_success'}}
            db.execute('UPDATE semantic_reparses SET state=?,result_json=?,reason=?,elapsed_ms=?,provider_diagnostics_json=? '
                       'WHERE session_id=? AND utterance_id=?',
                       (state, parsed.model_dump_json() if parsed else None, reason, elapsed,
                        json.dumps(safe), sid, uid))
            attempt = db.execute('SELECT * FROM semantic_reparses WHERE session_id=? AND utterance_id=?', (sid, uid)).fetchone()
            if state != 'ready':
                return output(reason, meta={'state': state, 'elapsed_ms': elapsed})
        # A validated model response is a durable checkpoint; commit can be retried without network IO.
        snapshot = ContextRead.model_validate_json(attempt['context_json'])
        request = UtteranceIn(utterance_id=uid, transcript=row['transcript'], source_type=row['source_type'],
                              speaker_status=row['speaker_status'], context_version=snapshot.version)
        parsed = SemanticResult.model_validate_json(attempt['result_json'])
        # Responses checkpointed by an older binary must pass current safety rules too.
        original = SemanticResult.model_validate_json(attempt['original_json'])
        parsed = context_rebind(row['transcript'], snapshot, parsed)
        reason = result_reason(row['transcript'], snapshot, original, parsed)
        if reason:
            db.execute("UPDATE semantic_reparses SET state='blocked',reason=? WHERE session_id=? AND utterance_id=?",
                       (reason, sid, uid))
            return output(reason, meta={'state':'blocked','elapsed_ms':attempt['elapsed_ms']})
        class Saved:
            def parse(self, transcript, context):
                return parsed
        result = service._process(db, Saved(), sid, request, recovery=attempt['pending_id'])
        return result.model_copy(update={'reparse': {'state': 'applied' if result.decision == 'COMMIT' else 'blocked',
                                                    'elapsed_ms': attempt['elapsed_ms']}})
    except Exception:
        db.rollback()
        raise
