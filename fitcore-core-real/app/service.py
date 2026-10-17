import hashlib
import json
import math
import sqlite3
from uuid import uuid4
from app import events
from app import context_safety
from app.semantic.coverage import NEGATIVE, check_coverage
from app.semantic.grounding import derive_switch_completion, verify_numeric_evidence, WEIGHT_UNIT
from app.correction import (ADD_MARKER, correction_old_value, correction_set_numbers,
                           current_correction, explicit_set_numbers, semantics_conflict)
from app.schemas import (ContextRead, FactRead, PendingRead, PendingResolutionIn, PendingResolutionOut,
                         ReviewRead, SemanticOperation, SemanticResult, SessionCreate, UtteranceIn, UtteranceOut)


class DomainReject(Exception):
    pass


class DomainDefer(Exception):
    pass


ALIASES = {"bench_press": "bench_press", "dumbbell_fly": "dumbbell_fly", "squat": "squat"}


def context(db, sid):
    row = db.execute("SELECT id,version,status,active_exercise_instance_id,last_completed_set_id,"
                     "last_reported_weight,next_set_plan FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None
    exercise = db.execute("SELECT exercise_name FROM exercise_instances WHERE id=?",
                          (row["active_exercise_instance_id"],)).fetchone()
    return ContextRead(session_id=row["id"], active_exercise_name=exercise[0] if exercise else None,
                       **{k: row[k] for k in row.keys() if k != "id"})


def start(db, request: SessionCreate):
    sid = request.session_id or str(uuid4())
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("INSERT INTO sessions(id) VALUES (?)", (sid,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return context(db, sid)


def number(value, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise DomainReject("INVALID_" + name)
    if integer and (value != int(value) or value < 1 or value > 100):
        raise DomainReject("INVALID_" + name)
    if not integer and (value <= 0 or value > 1000):
        raise DomainReject("INVALID_" + name)
    return value


def select_exercise(db, sid, name):
    if name not in ALIASES:
        raise DomainDefer("UNKNOWN_EXERCISE")
    ctx = context(db, sid)
    current = db.execute("SELECT exercise_name FROM exercise_instances WHERE id=?",
                         (ctx.active_exercise_instance_id,)).fetchone()
    if current and current[0] == name:
        return ctx.active_exercise_instance_id
    sequence = db.execute("SELECT COUNT(*)+1 FROM exercise_instances WHERE session_id=?", (sid,)).fetchone()[0]
    eid = db.execute("INSERT INTO exercise_instances(session_id,exercise_name,sequence) VALUES (?,?,?)",
                     (sid, name, sequence)).lastrowid
    db.execute("UPDATE sessions SET active_exercise_instance_id=?,last_completed_set_id=NULL,"
               "last_reported_weight=NULL,next_set_plan=NULL WHERE id=?", (eid, sid))
    return eid


def apply(db, sid, uid, op, *, allow_closed_set=False, correction_set_numbers=None, recovering_pending=None,
          correction_text=''):
    ctx = context(db, sid)
    if ctx.status != "active" and not (allow_closed_set and op.intent == "SET_COMPLETED" and ctx.status == "ended"):
        raise DomainReject("SESSION_CLOSED")
    if op.intent == "EXERCISE_SWITCH":
        if not op.exercise:
            raise DomainDefer("MISSING_EXERCISE")
        eid = select_exercise(db, sid, op.exercise)
        return {"type": op.intent, "exercise_instance_id": eid, "exercise": op.exercise}
    if op.intent == "SET_COMPLETED":
        # An explicit human confirmation supplies a complete fact value and
        # never relies on implicit context inheritance, so the same-instance
        # unresolved-pending guard must not block it (the pending being
        # confirmed is excluded below; sibling pendings stay open untouched).
        explicit_confirmation = recovering_pending is not None and op.weight_kg is not None
        if not explicit_confirmation and (op.exercise is None or op.weight_kg is None) and db.execute(
                "SELECT 1 FROM pending_operations WHERE session_id=? AND status='open' "
                "AND reason IN ('SEMANTIC_COVERAGE_INCOMPLETE','UNRELIABLE_CONTEXT_REFERENCE',"
                "'ASR_NUMERIC_AMBIGUITY_REQUIRES_CONFIRMATION','NUMERIC_EVIDENCE_WEIGHT_NOT_IN_TEXT',"
                "'AMBIGUOUS_WEIGHT_REFERENCE') AND id!=? "
                "AND (exercise_instance_id IS NULL OR exercise_instance_id=?) LIMIT 1",
                (sid, recovering_pending or -1, ctx.active_exercise_instance_id)).fetchone():
            raise DomainDefer('UNRESOLVED_SEMANTIC_CONTEXT')
        if (not allow_closed_set and
                context_safety.dependencies(db,sid,uid,ctx,exclude_pending=recovering_pending) and
                not context_safety.independent(correction_text,op,ctx)):
            raise DomainDefer('UNRESOLVED_CONTEXT_DEPENDENCY')
        reference_reason=context_safety.reference_reason(correction_text,op,ctx) if not allow_closed_set else None
        if reference_reason:
            raise DomainDefer(reference_reason)
        if op.exercise:
            select_exercise(db, sid, op.exercise)
            ctx = context(db, sid)
        if ctx.active_exercise_instance_id is None:
            raise DomainDefer("MISSING_EXERCISE")
        reps = number(op.reps, "REPS", True)
        if op.weight_kg is not None:
            weight = number(op.weight_kg, "WEIGHT")
            # A weight the model filled without any unit evidence in the text
            # is only trustworthy as an inheritance of the verified frame when
            # it equals the last reported weight; record that provenance
            # honestly instead of stamping a guessed value as EXPLICIT.
            if (correction_text and not WEIGHT_UNIT.search(correction_text)
                    and ctx.last_reported_weight is not None
                    and weight == ctx.last_reported_weight):
                provenance, source_fact = "INHERITED", ctx.last_completed_set_id
            else:
                provenance, source_fact = "EXPLICIT", None
        else:
            if ctx.last_reported_weight is None:
                raise DomainDefer("MISSING_WEIGHT")
            if ctx.next_set_plan is not None and ctx.next_set_plan != ctx.last_reported_weight:
                raise DomainDefer("AMBIGUOUS_WEIGHT_REFERENCE")
            weight = ctx.last_reported_weight
            provenance, source_fact = "INHERITED", ctx.last_completed_set_id
        idx = db.execute("SELECT COALESCE(MAX(set_number),0)+1 FROM sets WHERE exercise_instance_id=?",
                         (ctx.active_exercise_instance_id,)).fetchone()[0]
        if op.set_number is not None and op.set_number != idx:
            raise DomainDefer("SET_NUMBER_CONFLICT")
        fid = db.execute("INSERT INTO sets(exercise_instance_id,set_number,weight_kg,reps,"
                         "weight_provenance,weight_source_fact_id,source_utterance_id) VALUES (?,?,?,?,?,?,?)",
                         (ctx.active_exercise_instance_id, idx, weight, reps, provenance, source_fact, uid)).lastrowid
        db.execute("UPDATE sessions SET last_completed_set_id=?,last_reported_weight=?,next_set_plan=NULL WHERE id=?",
                   (fid, weight, sid))
        db.execute("UPDATE next_set_plans SET status='consumed' WHERE exercise_instance_id=? AND status='active'",
                   (ctx.active_exercise_instance_id,))
        return {"type": op.intent, "id": fid, "exercise_instance_id": ctx.active_exercise_instance_id,
                "exercise": db.execute("SELECT exercise_name FROM exercise_instances WHERE id=?",
                                       (ctx.active_exercise_instance_id,)).fetchone()[0],
                "set_number": idx, "weight_kg": weight, "reps": reps, "weight_provenance": provenance}
    if op.intent == "TRAINING_NOTE_ADDED":
        if not op.note:
            raise DomainDefer("MISSING_NOTE")
        db.execute("INSERT INTO training_notes(session_id,set_id,text,source_utterance_id) VALUES (?,?,?,?)",
                   (sid, ctx.last_completed_set_id, op.note, uid))
        return {"type": op.intent, "text": op.note}
    if op.intent == "NEXT_SET_PLAN_CHANGED":
        if ctx.active_exercise_instance_id is None:
            raise DomainDefer("MISSING_EXERCISE")
        if (op.weight_kg is None) == (op.weight_delta_kg is None):
            raise DomainReject("PLAN_REQUIRES_ABSOLUTE_OR_DELTA")
        if op.weight_delta_kg is not None:
            if ctx.last_reported_weight is None:
                raise DomainDefer("MISSING_PLAN_BASE")
            if not math.isfinite(op.weight_delta_kg):
                raise DomainReject("INVALID_DELTA")
            weight = ctx.last_reported_weight + op.weight_delta_kg
        else:
            weight = op.weight_kg
        number(weight, "WEIGHT")
        db.execute("UPDATE next_set_plans SET status='superseded' WHERE exercise_instance_id=? AND status='active'",
                   (ctx.active_exercise_instance_id,))
        db.execute("INSERT INTO next_set_plans(exercise_instance_id,weight_kg,base_set_id,source_utterance_id) "
                   "VALUES (?,?,?,?)", (ctx.active_exercise_instance_id, weight, ctx.last_completed_set_id, uid))
        db.execute("UPDATE sessions SET next_set_plan=? WHERE id=?", (weight, sid))
        return {"type": op.intent, "weight_kg": weight}
    if op.intent == "CORRECTION":
        if ADD_MARKER.search(correction_text):
            # Supplement semantics: "还漏记了一组" adds a set, it never
            # corrects an existing one. Routed to the missed-set-add decision.
            raise DomainDefer('MISSED_SET_ADD')
        target_number = op.set_number
        if correction_set_numbers:
            if len(correction_set_numbers) != 1:
                raise DomainDefer('AMBIGUOUS_CORRECTION_TARGET')
            spoken_number = next(iter(correction_set_numbers))
            if target_number is not None and target_number != spoken_number:
                raise DomainDefer('CORRECTION_TARGET_SET_NUMBER_CONFLICT')
            target_number = spoken_number
        if op.reps is None and op.weight_kg is None:
            raise DomainDefer("EMPTY_CORRECTION")
        target_id = op.target_set_id
        if not correction_set_numbers and current_correction(correction_text):
            # An uncommitted recent utterance may refer to a missing set, not the
            # last formal fact. A model-supplied ID cannot resolve that ambiguity.
            if db.execute("SELECT 1 FROM pending_operations WHERE session_id=? AND status='open' "
                          "AND exercise_instance_id=? AND (? IS NULL OR id!=?) LIMIT 1",
                          (sid,ctx.active_exercise_instance_id,recovering_pending,recovering_pending)).fetchone():
                raise DomainDefer('CORRECTION_TARGET_UNCONFIRMED_WITH_PENDING')
            if target_id is None:
                target_id = ctx.last_completed_set_id
            elif target_id != ctx.last_completed_set_id:
                raise DomainDefer('CORRECTION_TARGET_RECENCY_CONFLICT')
        if target_id is not None:
            candidates = db.execute("SELECT s.*,e.exercise_name AS target_exercise_name FROM sets s JOIN exercise_instances e "
                                    "ON e.id=s.exercise_instance_id WHERE e.session_id=? AND s.id=? AND s.is_voided=0",
                                    (sid, target_id)).fetchall()
        elif target_number is not None and ctx.active_exercise_instance_id is not None:
            candidates = db.execute("SELECT s.*,e.exercise_name AS target_exercise_name FROM sets s "
                                    "JOIN exercise_instances e ON e.id=s.exercise_instance_id "
                                    "WHERE e.session_id=? AND s.exercise_instance_id=? AND s.set_number=? AND s.is_voided=0",
                                    (sid, ctx.active_exercise_instance_id, target_number)).fetchall()
        else:
            # No target from the model and no spoken evidence: the correction is
            # acceptable only when the active instance holds exactly one
            # non-voided set AND no unresolved pending (which may stand for a
            # not-yet-confirmed set). Anything else is deferred, never guessed.
            if ctx.active_exercise_instance_id is None:
                raise DomainDefer("MISSING_CORRECTION_TARGET")
            if db.execute("SELECT 1 FROM pending_operations WHERE session_id=? AND status='open' "
                          "AND (exercise_instance_id IS NULL OR exercise_instance_id=?) LIMIT 1",
                          (sid, ctx.active_exercise_instance_id)).fetchone():
                raise DomainDefer("AMBIGUOUS_CORRECTION_TARGET")
            rows = db.execute("SELECT id FROM sets WHERE exercise_instance_id=? AND is_voided=0",
                              (ctx.active_exercise_instance_id,)).fetchall()
            if len(rows) != 1:
                raise DomainDefer("MISSING_CORRECTION_TARGET" if not rows else "AMBIGUOUS_CORRECTION_TARGET")
            candidates = db.execute("SELECT s.*,e.exercise_name AS target_exercise_name FROM sets s "
                                    "JOIN exercise_instances e ON e.id=s.exercise_instance_id "
                                    "WHERE e.session_id=? AND s.id=? AND s.is_voided=0",
                                    (sid, rows[0]['id'])).fetchall()
        if not candidates:
            raise DomainDefer("CORRECTION_TARGET_NOT_FOUND")
        if len(candidates) != 1:
            raise DomainDefer("AMBIGUOUS_CORRECTION_TARGET")
        old = candidates[0]
        # If the utterance names the target's OLD value ("那个8个其实是9个"),
        # the binding must be verifiable against it. A mismatch is only
        # tolerated when the claimed value matches exactly ONE set in the same
        # instance: the latest-set binding is then the R7 recency contract.
        # Zero or several matches leave the reference unresolved — the
        # correction must not modify an arbitrary set.
        spoken_old = correction_old_value(correction_text)
        if spoken_old is not None:
            field, value = spoken_old
            current = old['reps'] if field == 'reps' else old['weight_kg']
            gate_field = (field == 'reps' and op.reps is not None) or \
                         (field == 'weight' and op.weight_kg is not None)
            if current != value and gate_field:
                column = 'reps' if field == 'reps' else 'weight_kg'
                matching = db.execute(
                    f"SELECT COUNT(*) FROM sets WHERE exercise_instance_id=? "
                    f"AND is_voided=0 AND {column}=?",
                    (old['exercise_instance_id'], value)).fetchone()[0]
                if matching != 1:
                    raise DomainDefer('AMBIGUOUS_CORRECTION_TARGET')
        # All supplied target evidence must agree before any mutation or audit entry.
        # An explicit ID is not permission to silently disregard a different set number.
        if target_number is not None and target_number != old['set_number']:
            raise DomainDefer('CORRECTION_TARGET_SET_NUMBER_CONFLICT')
        if op.exercise is not None and op.exercise != old['target_exercise_name']:
            raise DomainDefer('CORRECTION_TARGET_EXERCISE_CONFLICT')
        if op.exercise_instance_id is not None and op.exercise_instance_id != old['exercise_instance_id']:
            raise DomainDefer('CORRECTION_TARGET_INSTANCE_CONFLICT')
        # Natural-language corrections have no independently verified instance binding.
        # Cross-instance targets need confirmation, even when exercise names repeat.
        if old['exercise_instance_id'] != ctx.active_exercise_instance_id:
            raise DomainDefer('CORRECTION_TARGET_INSTANCE_CONFLICT')
        # Target identity must be supported by the utterance itself. Without an
        # explicit spoken ordinal or a recent-set reference, model-supplied
        # target_set_id / set_number values are speculation: accepting them could
        # silently modify any set, including a non-recent one. An explicit
        # ordinal spoken anywhere in the utterance is real user evidence and
        # must match the resolved target exactly. The only exception is a
        # proven-unique set: when the instance holds exactly one non-voided set
        # and no unresolved pending, that single set is the sole possible
        # target and the correction may bind to it.
        if not correction_set_numbers and not current_correction(correction_text):
            spoken = explicit_set_numbers(correction_text)
            if not (len(spoken) == 1 and next(iter(spoken)) == old['set_number']):
                unique_rows = db.execute("SELECT id FROM sets WHERE exercise_instance_id=? AND is_voided=0",
                                         (ctx.active_exercise_instance_id,)).fetchall()
                pending_open = db.execute(
                    "SELECT 1 FROM pending_operations WHERE session_id=? AND status='open' "
                    "AND (exercise_instance_id IS NULL OR exercise_instance_id=?) LIMIT 1",
                    (sid, ctx.active_exercise_instance_id)).fetchone()
                if not (len(unique_rows) == 1 and not pending_open and unique_rows[0]['id'] == old['id']):
                    raise DomainDefer('AMBIGUOUS_CORRECTION_TARGET')
        weight = number(op.weight_kg, "WEIGHT") if op.weight_kg is not None else old["weight_kg"]
        reps = number(op.reps, "REPS", True) if op.reps is not None else old["reps"]
        db.execute("UPDATE sets SET weight_kg=?,reps=? WHERE id=?", (weight, reps, old["id"]))
        log_change(db, sid, uid, "set", old["id"],
                   {"weight_kg": old["weight_kg"], "reps": old["reps"]},
                   {"weight_kg": weight, "reps": reps})
        affected = []
        if weight != old["weight_kg"]:
            # Only explicitly inherited descendants are recalculated.
            # An explicit later weight is an independent fact and stays intact.
            frontier = [old["id"]]
            while frontier:
                parent = frontier.pop(0)
                children = db.execute("SELECT * FROM sets WHERE weight_source_fact_id=? AND is_voided=0",
                                      (parent,)).fetchall()
                parent_weight = db.execute("SELECT weight_kg FROM sets WHERE id=?", (parent,)).fetchone()[0]
                for child in children:
                    if child["weight_provenance"] != "INHERITED":
                        raise DomainDefer("DEPENDENCY_CONFLICT")
                    db.execute("UPDATE sets SET weight_kg=? WHERE id=?", (parent_weight, child["id"]))
                    log_change(db, sid, uid, "inherited_set", child["id"],
                               {"weight_kg": child["weight_kg"]}, {"weight_kg": parent_weight})
                    affected.append(child["id"])
                    frontier.append(child["id"])
            impacted = [old["id"], *affected]
            placeholders = ",".join("?" for _ in impacted)
            plans = db.execute(f"SELECT * FROM next_set_plans WHERE base_set_id IN ({placeholders}) "
                               "AND status='active'", impacted).fetchall()
            for plan in plans:
                db.execute("UPDATE next_set_plans SET status='invalidated' WHERE id=?", (plan["id"],))
                db.execute("UPDATE sessions SET next_set_plan=NULL WHERE id=? AND active_exercise_instance_id=?",
                           (sid, plan["exercise_instance_id"]))
                log_change(db, sid, uid, "plan_invalidated", plan["id"],
                           {"weight_kg": plan["weight_kg"]}, {"status": "invalidated"})
            latest = db.execute("SELECT weight_kg FROM sets WHERE id=?", (ctx.last_completed_set_id,)).fetchone() \
                if ctx.last_completed_set_id is not None else None
            if latest:
                db.execute("UPDATE sessions SET last_reported_weight=? WHERE id=?", (latest[0], sid))
        return {"type": op.intent, "target_set_id": old["id"], "old_reps": old["reps"],
                "reps": reps, "old_weight_kg": old["weight_kg"], "weight_kg": weight,
                "affected_set_ids": affected}
    if op.intent == "SESSION_END":
        db.execute("UPDATE sessions SET status='ended',ended_at=CURRENT_TIMESTAMP WHERE id=?", (sid,))
        return {"type": op.intent}
    raise DomainDefer("UNIMPLEMENTED_" + op.intent)


def log_change(db, sid, uid, target_type, target_id, before, after):
    db.execute("INSERT INTO change_log(session_id,target_type,target_id,before_json,after_json,source_utterance_id) "
               "VALUES (?,?,?,?,?,?)",
               (sid, target_type, target_id, json.dumps(before), json.dumps(after), uid))


def process(db, provider, sid, request: UtteranceIn):
    result = _process(db, provider, sid, request)
    if result and result.decision in {'DEFER', 'DUPLICATE'} and callable(getattr(provider, 'reparse', None)):
        from app.semantic.reparse import recover, eligible
        resumable = False
        if result.decision == 'DUPLICATE':
            # Crash between initial Pending and claim, or between ready checkpoint and commit.
            # Failed/uncertain remote calls never gain a second automatic model attempt.
            row = db.execute("SELECT p.reason,r.state FROM pending_operations p LEFT JOIN semantic_reparses r "
                             "ON r.session_id=p.session_id AND r.utterance_id=p.utterance_id "
                             "WHERE p.session_id=? AND p.utterance_id=? AND p.status='open'",
                             (sid, request.utterance_id)).fetchone()
            resumable = bool(row and eligible(row['reason']) and row['state'] in (None, 'ready'))
        if eligible(result.reason) or resumable:
            return recover(db, provider, sid, request.utterance_id)
    return result


def _process(db, provider, sid, request: UtteranceIn, *, recovery=None):
    ctx = context(db, sid)
    if ctx is None:
        return None
    fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    old = db.execute("SELECT request_hash FROM utterances WHERE session_id=? AND id=?",
                     (sid, request.utterance_id)).fetchone()
    if old and recovery is None:
        return UtteranceOut(utterance_id=request.utterance_id,
                            decision="DUPLICATE" if old[0] == fingerprint else "ID_CONFLICT",
                            context_version=ctx.version)
    if request.speaker_status == "NON_TARGET":
        parsed = None
    elif request.speaker_status == "UNCERTAIN":
        parsed = None
    else:
        try:
            parsed = provider.parse(request.transcript, ctx)
            if not isinstance(parsed, SemanticResult):
                raise ValueError("INVALID_SEMANTIC_RESULT")
        except Exception:
            events.core_result(db, sid, request.utterance_id, 'ERROR', ctx.version, [], 'SEMANTIC_PROVIDER_ERROR')
            return UtteranceOut(utterance_id=request.utterance_id, decision="ERROR",
                                context_version=ctx.version, reason="SEMANTIC_PROVIDER_ERROR")
    diagnostics = [d.to_dict() for d in check_coverage(request.transcript, parsed, ctx)] if parsed else []
    db.execute("BEGIN IMMEDIATE")
    try:
        old = db.execute("SELECT request_hash FROM utterances WHERE session_id=? AND id=?",
                         (sid, request.utterance_id)).fetchone()
        if old and recovery is None:
            db.rollback()
            return UtteranceOut(utterance_id=request.utterance_id,
                                decision="DUPLICATE" if old[0] == fingerprint else "ID_CONFLICT",
                                context_version=context(db, sid).version)
        live = context(db, sid)
        if recovery is not None:
            attempt = db.execute('SELECT * FROM semantic_reparses WHERE session_id=? AND utterance_id=?',
                                 (sid, request.utterance_id)).fetchone()
            pending_row = db.execute('SELECT * FROM pending_operations WHERE id=? AND session_id=?',
                                     (recovery, sid)).fetchone()
            if (attempt is None or attempt['state'] != 'ready' or pending_row is None or
                    pending_row['status'] != 'open' or attempt['pending_id'] != recovery):
                db.rollback()
                return UtteranceOut(utterance_id=request.utterance_id, decision='STALE_CONTEXT',
                                    context_version=live.version, reason='RECOVERY_STATE_CHANGED')
        barrier = db.execute('SELECT 1 FROM session_finishes WHERE session_id=?', (sid,)).fetchone()
        if barrier:
            accepted = db.execute('SELECT final_text,source_type,speaker_status FROM audio_jobs WHERE session_id=? AND utterance_id=?',
                                  (sid, request.utterance_id)).fetchone()
            if (accepted is None or accepted['final_text'] != request.transcript or
                accepted['source_type'] != request.source_type or accepted['speaker_status'] != request.speaker_status.value):
                db.rollback()
                return UtteranceOut(utterance_id=request.utterance_id, decision='REJECT',
                                    context_version=live.version, reason='AUDIO_FINISH_BARRIER')
        if live.status != "active":
            db.rollback()
            return UtteranceOut(utterance_id=request.utterance_id, decision="REJECT",
                                context_version=live.version, reason="SESSION_CLOSED")
        if live.version != ctx.version or (request.context_version is not None and request.context_version != live.version):
            db.rollback()
            return UtteranceOut(utterance_id=request.utterance_id, decision="STALE_CONTEXT",
                                context_version=live.version, reason="REPARSE_REQUIRED")
        decision, reason, committed = "COMMIT", None, []
        db.execute("SAVEPOINT facts")
        try:
            if request.speaker_status == "NON_TARGET":
                decision, reason = "REJECT", "NON_TARGET"
            elif request.speaker_status == "UNCERTAIN":
                raise DomainDefer("SOURCE_UNCERTAIN")
            else:
                if diagnostics and parsed is not None:
                    derived = derive_switch_completion(request.transcript, live, parsed)
                    if derived is not None:
                        parsed = derived
                        diagnostics = check_coverage(request.transcript, parsed, live)
                if diagnostics:
                    raise DomainDefer('SEMANTIC_COVERAGE_INCOMPLETE')
                if parsed.ambiguities:
                    raise DomainDefer(parsed.ambiguities[0])
                if not parsed.operations:
                    decision, reason = "REJECT", "NO_TRAINING_OPERATION"
                else:
                    indices = [op.operation_index for op in parsed.operations]
                    if indices != list(range(len(indices))):
                        raise DomainReject("INVALID_OPERATION_ORDER")
                    numeric_reason=verify_numeric_evidence(request.transcript,live,parsed)
                    if numeric_reason:
                        raise DomainDefer(numeric_reason)
                    for op in parsed.operations:
                        if op.who == "unknown" or op.when == "uncertain":
                            raise DomainDefer("UNCERTAIN_SUBJECT_OR_TIME")
                        if op.who == "other" or op.when == "historical" or op.modality in {"hypothetical", "negative", "question"}:
                            continue
                        conflict = semantics_conflict(request.transcript, op, live)
                        if conflict:
                            raise DomainDefer(conflict)
                        if op.intent == "SET_COMPLETED" and (op.when != "current" or op.modality != "fact"):
                            raise DomainReject("INVALID_SET_MODALITY")
                        if op.intent == "NEXT_SET_PLAN_CHANGED" and op.modality != "plan":
                            raise DomainReject("INVALID_PLAN_MODALITY")
                        if op.intent == 'SESSION_END' and db.execute('SELECT 1 FROM audio_jobs WHERE session_id=? LIMIT 1', (sid,)).fetchone():
                            raise DomainDefer('AUDIO_FINISH_REQUIRED')
                        committed.append(apply(db, sid, request.utterance_id, op,
                                               correction_set_numbers=correction_set_numbers(request.transcript),
                                               recovering_pending=recovery, correction_text=request.transcript))
                    if not committed:
                        decision, reason = "REJECT", "NO_CURRENT_SELF_OPERATION"
        except (DomainReject, DomainDefer, TypeError) as exc:
            db.execute("ROLLBACK TO facts")
            committed = []
            decision = "DEFER" if isinstance(exc, DomainDefer) else "REJECT"
            reason = str(exc)
            if (decision == "DEFER" and recovery is None and reason in AUTO_MERGE_DEFER_REASONS
                    and parsed is not None):
                # Multi-turn slot merge: the current one-slot utterance may
                # complete exactly one complementary pending ON THE SAME
                # instance. The merged fact still passes the shared numeric
                # gate and the Fact Engine below; any failure keeps the defer.
                candidate = auto_merge_candidate(db, sid, live, request, parsed)
                if candidate is not None:
                    pending_row, merged_op, merged_text = candidate
                    merged = SemanticResult(operations=[merged_op], ambiguities=[])
                    db.execute("SAVEPOINT autorecover")
                    try:
                        if verify_numeric_evidence(merged_text, live, merged):
                            raise DomainDefer(reason)
                        conflict = semantics_conflict(merged_text, merged_op, live)
                        if conflict:
                            raise DomainDefer(conflict)
                        committed = [apply(db, sid, request.utterance_id, merged_op,
                                           recovering_pending=pending_row['id'],
                                           correction_text=merged_text)]
                        db.execute("UPDATE pending_operations SET status='resolved' WHERE id=?",
                                   (pending_row['id'],))
                        db.execute("INSERT INTO pending_resolutions(session_id,pending_id,resolution_id,"
                                   "request_hash,action,resulting_version) VALUES (?,?,?,?,?,?)",
                                   (sid, pending_row['id'], 'auto:' + request.utterance_id,
                                    hashlib.sha256(merged.model_dump_json().encode()).hexdigest(),
                                    'auto_slot_merge', live.version + 1))
                        db.execute("RELEASE autorecover")
                        decision, reason = "COMMIT", None
                        events.append(db, sid, 'resolution:auto:' + request.utterance_id,
                                      'FACT_COMMITTED', request.utterance_id,
                                      {'resolution_id': 'auto:' + request.utterance_id,
                                       'decision': 'RESOLVED',
                                       'matched_pending_id': pending_row['id'],
                                       'source_utterance_ids': [pending_row['utterance_id'],
                                                                request.utterance_id],
                                       'recovery_kind': 'auto_slot_merge',
                                       'context_version': live.version + 1,
                                       'committed': committed})
                    except (DomainReject, DomainDefer, TypeError):
                        db.execute("ROLLBACK TO autorecover")
                        db.execute("RELEASE autorecover")
            elif (decision == "DEFER" and recovery is None and parsed is not None
                  and len(parsed.operations) == 1
                  and parsed.operations[0].intent == 'CORRECTION'
                  and reason == 'MISSED_SET_ADD'):
                # Missed-set supplement ("还漏记了一组"): with complete
                # evidence a new set may be added; never a correction.
                cop = parsed.operations[0]
                if cop.weight_kg is not None or cop.reps is not None:
                    merged_op = SemanticOperation(operation_index=0, intent='SET_COMPLETED',
                                                  who='self', when='current', modality='fact',
                                                  exercise=cop.exercise or live.active_exercise_name,
                                                  weight_kg=cop.weight_kg, reps=cop.reps)
                    merged = SemanticResult(operations=[merged_op], ambiguities=[])
                    db.execute("SAVEPOINT missedadd")
                    try:
                        if verify_numeric_evidence(request.transcript, live, merged):
                            raise DomainDefer(reason)
                        if semantics_conflict(request.transcript, merged_op, live):
                            raise DomainDefer(reason)
                        committed = [apply(db, sid, request.utterance_id, merged_op,
                                           correction_text=request.transcript)]
                        db.execute("RELEASE missedadd")
                        decision, reason = "COMMIT", None
                        events.append(db, sid, 'missed:' + request.utterance_id,
                                      'FACT_COMMITTED', request.utterance_id,
                                      {'decision': 'RESOLVED', 'recovery_kind': 'missed_set_add',
                                       'matched_pending_id': None,
                                       'source_utterance_ids': [request.utterance_id],
                                       'context_version': live.version + 1,
                                       'committed': committed})
                    except (DomainReject, DomainDefer, TypeError):
                        db.execute("ROLLBACK TO missedadd")
                        db.execute("RELEASE missedadd")
        db.execute("RELEASE facts")
        if recovery is not None:
            if decision == 'COMMIT':
                db.execute("UPDATE utterances SET processing_status='COMMIT',reason=NULL WHERE session_id=? AND id=?",
                           (sid, request.utterance_id))
                db.execute("UPDATE pending_operations SET status='resolved' WHERE id=?", (recovery,))
                db.execute('DELETE FROM semantic_operations WHERE session_id=? AND utterance_id=?',
                           (sid, request.utterance_id))
            else:
                parsed = None  # Preserve original Pending/output; attempt retains the rejected response.
        else:
            db.execute("INSERT INTO utterances(session_id,id,transcript,source_type,speaker_status,request_hash,"
                   "context_version_at_parse,processing_status,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                   (sid, request.utterance_id, request.transcript, request.source_type,
                    request.speaker_status.value, fingerprint, ctx.version, decision, reason))
        if parsed is not None:
            for op in parsed.operations:
                db.execute("INSERT INTO semantic_operations(session_id,utterance_id,operation_index,type,payload_json) "
                           "VALUES (?,?,?,?,?)", (sid, request.utterance_id, op.operation_index, op.intent,
                                                 op.model_dump_json()))
        if decision == "DEFER" and recovery is None:
            payload = parsed.model_dump() if parsed else {}
            if diagnostics:
                payload['coverage_diagnostics'] = diagnostics
            evidence=context_safety.hazard(request.transcript,payload.get('operations',[]))
            if evidence:
                payload['context_hazard']=evidence
            links=context_safety.dependencies(db,sid,request.utterance_id,ctx)
            if links:
                payload['upstream_context_dependencies']=links
            db.execute("INSERT INTO pending_operations(session_id,utterance_id,reason,payload_json,"
                       "context_version_at_defer,exercise_instance_id,last_completed_set_id) VALUES (?,?,?,?,?,?,?)",
                       (sid, request.utterance_id, reason, json.dumps(payload, ensure_ascii=False),
                        ctx.version, ctx.active_exercise_instance_id, ctx.last_completed_set_id))
        if decision == "COMMIT":
            db.execute("UPDATE sessions SET version=version+1 WHERE id=?", (sid,))
        if recovery is None:
            events.core_result(db, sid, request.utterance_id, decision, context(db, sid).version, committed, reason)
        else:
            outcome = UtteranceOut(utterance_id=request.utterance_id, decision=decision,
                context_version=context(db, sid).version, committed=committed, reason=reason,
                semantic_diagnostics=diagnostics)
            db.execute('UPDATE semantic_reparses SET state=?,outcome_json=?,reason=? '
                       'WHERE session_id=? AND utterance_id=?',
                       ('applied' if decision == 'COMMIT' else 'blocked', outcome.model_dump_json(), reason,
                        sid, request.utterance_id))
            if decision == 'COMMIT':
                events.append(db, sid, f'utterance:{request.utterance_id}:reparse:result',
                    'FACT_COMMITTED', request.utterance_id, outcome.model_dump())
        db.commit()
        return UtteranceOut(utterance_id=request.utterance_id, decision=decision,
                            context_version=context(db, sid).version, committed=committed, reason=reason,
                            semantic_diagnostics=diagnostics)
    except Exception:
        db.rollback()
        raise


def facts(db, sid):
    rows = db.execute("SELECT s.id,s.exercise_instance_id,e.exercise_name AS exercise,s.set_number,"
                      "s.weight_kg,s.reps,s.weight_provenance,s.weight_source_fact_id,s.source_utterance_id "
                      "FROM sets s JOIN exercise_instances e ON e.id=s.exercise_instance_id "
                      "WHERE e.session_id=? AND s.is_voided=0 ORDER BY s.id", (sid,))
    return [FactRead(**dict(r)) for r in rows]


def pending(db, sid):
    result = []
    for row in db.execute("SELECT id,utterance_id,reason,status,context_version_at_defer,"
                          "exercise_instance_id,last_completed_set_id,payload_json FROM pending_operations "
                          "WHERE session_id=? ORDER BY id", (sid,)):
        values = dict(row)
        payload = json.loads(values.pop('payload_json'))
        result.append(PendingRead(**values, semantic_diagnostics=payload.get('coverage_diagnostics', []),
                                  context_hazard=payload.get('context_hazard'),
                                  upstream_context_dependencies=payload.get('upstream_context_dependencies', [])))
    return result


def attach_asr_evidence(db, sid, uid, evidence) -> bool:
    """Persist second-pass ASR evidence onto the OPEN pending of an utterance.

    Writes pending_operations.payload_json['asr_evidence'] only; the pending
    stays open, the decision never changes and no fact is created. The field is
    intentionally not exposed through the current PendingRead HTTP contract
    (client-visible exposure requires a coordinated contract change).
    """
    row = db.execute("SELECT id,payload_json FROM pending_operations "
                     "WHERE session_id=? AND utterance_id=? AND status='open' "
                     "ORDER BY id DESC LIMIT 1", (sid, uid)).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(row['payload_json'])
    except (ValueError, TypeError):
        return False
    payload['asr_evidence'] = evidence
    db.execute("UPDATE pending_operations SET payload_json=? WHERE id=?",
               (json.dumps(payload, ensure_ascii=False), row['id']))
    return True


class PendingConflict(Exception):
    pass


AUTO_MERGE_DEFER_REASONS = {'MISSING_WEIGHT', 'NUMERIC_EVIDENCE_NO_CURRENT_FACT',
                            'UNRESOLVED_CONTEXT_DEPENDENCY'}


def auto_merge_candidate(db, sid, ctx, request, parsed):
    """Find the unique complementary OPEN pending for a one-slot completion.

    Multi-turn slot accumulation (A02 phase 2): when the current utterance
    supplies exactly one missing slot (weight OR reps) of a single SET op, and
    exactly ONE open pending on the same exercise instance carries the
    complementary slot, the pair is a merge candidate. Everything else —
    ambiguous pendings, cross-instance pendings, no matching pending — returns
    None and the caller keeps its safe defer.

    The merged fact is NOT trusted here: the caller must re-run the shared
    numeric gate and the Fact Engine on the merged text before committing.
    """
    if parsed.ambiguities or len(parsed.operations) != 1:
        return None
    if NEGATIVE.search(request.transcript):
        return None  # a negated count ("不是8个") is never a completion slot
    cur = parsed.operations[0]
    if (cur.intent != 'SET_COMPLETED' or cur.who != 'self' or cur.when != 'current'
            or cur.modality != 'fact'):
        return None
    has_weight = cur.weight_kg is not None
    has_reps = cur.reps is not None
    if has_weight == has_reps:
        return None  # complete facts need no merge; both-missing is not completable
    rows = db.execute("SELECT p.*, u.transcript AS pending_text FROM pending_operations p "
                      "JOIN utterances u ON u.session_id=p.session_id AND u.id=p.utterance_id "
                      "WHERE p.session_id=? AND p.status='open' "
                      "AND p.reason IN ('MISSING_WEIGHT','NUMERIC_EVIDENCE_NO_CURRENT_FACT') "
                      "AND (p.exercise_instance_id IS NULL OR p.exercise_instance_id=?) "
                      "ORDER BY p.id DESC", (sid, ctx.active_exercise_instance_id)).fetchall()
    candidates = []
    for row in rows:
        payload = json.loads(row['payload_json'])
        psets = [o for o in payload.get('operations', []) if o.get('intent') == 'SET_COMPLETED']
        if len(psets) != 1:
            continue
        p = psets[0]
        p_weight = p.get('weight_kg') is not None
        p_reps = p.get('reps') is not None
        if not ((p_weight and has_reps) or (p_reps and has_weight)):
            continue  # complementary slots only
        weight = cur.weight_kg if has_weight else p.get('weight_kg')
        reps = cur.reps if has_reps else p.get('reps')
        exercise = cur.exercise or p.get('exercise') or ctx.active_exercise_name
        if not exercise:
            continue
        if p.get('exercise_instance_id') not in (None, ctx.active_exercise_instance_id):
            continue
        candidates.append((row, weight, reps, exercise))
    if len(candidates) != 1:
        return None
    row, weight, reps, exercise = candidates[0]
    merged_op = SemanticOperation(operation_index=0, intent='SET_COMPLETED', who='self',
                                  when='current', modality='fact', exercise=exercise,
                                  weight_kg=weight, reps=reps)
    merged_text = f"{row['pending_text']}，{request.transcript}"
    return row, merged_op, merged_text


def resolve_pending(db, sid: str, pending_id: int, request: PendingResolutionIn):
    fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    db.execute("BEGIN IMMEDIATE")
    try:
        previous = db.execute("SELECT * FROM pending_resolutions WHERE session_id=? AND resolution_id=?",
                              (sid, request.resolution_id)).fetchone()
        if previous:
            if previous["pending_id"] != pending_id or previous["request_hash"] != fingerprint:
                raise PendingConflict("RESOLUTION_ID_CONFLICT")
            db.commit()
            return PendingResolutionOut(pending_id=pending_id, resolution_id=request.resolution_id,
                                        decision="DUPLICATE", context_version=context(db, sid).version)
        row = db.execute("SELECT p.*,u.speaker_status FROM pending_operations p "
                         "JOIN utterances u ON u.session_id=p.session_id AND u.id=p.utterance_id "
                         "WHERE p.session_id=? AND p.id=?", (sid, pending_id)).fetchone()
        if row is None:
            raise PendingConflict("PENDING_NOT_FOUND")
        if row["status"] != "open":
            raise PendingConflict("PENDING_ALREADY_CLOSED")
        ctx = context(db, sid)
        if ctx.status not in {"active", "ended"}:
            raise PendingConflict("SESSION_CLOSED")
        if ctx.version != request.context_version:
            raise PendingConflict("STALE_CONTEXT")
        committed = []
        if request.action == "confirm_set":
            if row["speaker_status"] != "VERIFIED_USER":
                raise PendingConflict("SOURCE_NOT_VERIFIED")
            if row["reason"] not in {"BARE_NUMBER", "AMBIGUOUS_WEIGHT_REFERENCE", "MISSING_WEIGHT",
                                     "ASR_NUMERIC_AMBIGUITY_REQUIRES_CONFIRMATION",
                                     "NUMERIC_EVIDENCE_WEIGHT_NOT_IN_TEXT",
                                     "UNRELIABLE_CONTEXT_REFERENCE"}:
                raise PendingConflict("PENDING_NOT_CONFIRMABLE_AS_SET")
            if row["exercise_instance_id"] is None or ctx.active_exercise_instance_id != row["exercise_instance_id"]:
                raise PendingConflict("EXERCISE_CHANGED")
            if ctx.last_completed_set_id != row["last_completed_set_id"]:
                raise PendingConflict("NEW_SET_SINCE_PENDING")
            if request.weight_kg is None or request.reps is None:
                raise PendingConflict("FULL_SET_CONFIRMATION_REQUIRED")
            try:
                number(request.weight_kg, "WEIGHT")
                number(request.reps, "REPS", True)
            except DomainReject as exc:
                raise PendingConflict(str(exc)) from exc
            op = SemanticOperation(operation_index=0, intent="SET_COMPLETED",
                                   weight_kg=request.weight_kg, reps=request.reps)
            committed.append(apply(db, sid, request.resolution_id, op, allow_closed_set=True,
                                   recovering_pending=pending_id))
            db.execute("UPDATE sessions SET version=version+1 WHERE id=?", (sid,))
            status, decision = "resolved", "RESOLVED"
        else:
            if request.weight_kg is not None or request.reps is not None:
                raise PendingConflict("DISMISS_MUST_NOT_INCLUDE_FACT")
            status, decision = "dismissed", "DISMISSED"
        db.execute("UPDATE pending_operations SET status=? WHERE id=?", (status, pending_id))
        version = context(db, sid).version
        db.execute("INSERT INTO pending_resolutions(session_id,pending_id,resolution_id,request_hash,action,resulting_version) "
                   "VALUES (?,?,?,?,?,?)", (sid, pending_id, request.resolution_id, fingerprint, request.action, version))
        events.append(db, sid, f'resolution:{request.resolution_id}',
                      'FACT_COMMITTED' if committed else 'INPUT_REJECTED', row['utterance_id'],
                      {'resolution_id': request.resolution_id, 'decision': decision,
                       'context_version': version, 'committed': committed, 'pending_id': pending_id})
        db.commit()
        return PendingResolutionOut(pending_id=pending_id, resolution_id=request.resolution_id,
                                    decision=decision, context_version=version, committed=committed)
    except Exception:
        db.rollback()
        raise


def review(db, sid):
    records = facts(db, sid)
    return ReviewRead(session_id=sid, context_version=context(db, sid).version,
                      total_sets=len(records), total_reps=sum(f.reps for f in records),
                      volume_kg_reps=sum(f.weight_kg * f.reps for f in records),
                      pending_count=sum(p.status == "open" for p in pending(db, sid)), facts=records)
