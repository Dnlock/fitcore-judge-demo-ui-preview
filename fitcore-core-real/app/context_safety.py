"""Pending evidence is a veto on inference, never a source of confirmed values."""
import json
import re
from app.schemas import SemanticResult, SemanticOperation
from app.semantic.coverage import check_coverage, COUNT, HISTORY, PLAN, CONDITION, NEGATIVE, OTHER, QUESTION
from app.semantic.grounding import EXERCISES, WEIGHT, WEIGHT_UNIT, SIGNED_WEIGHT, SIGNED_COUNT, NUM, numeric

SWITCH = re.compile(r'换|切换|改做|开始|接着做')
CHANGE = re.compile(r'重量|加重|减重|调到|换成|减|加')
REFERENCE = re.compile(r'这个重量|这重量|同样|一样|相同|原重量|还是|仍然')
CHINESE_NUMBER = re.compile(r'(?:[一二两三四五六七八九]百(?:零[一二两三四五六七八九]|[一二两三四五六七八九]十[一二三四五六七八九]?)?|[一二两三四五六七八九]?十[一二三四五六七八九]?|[零〇一二两三四五六七八九])')


def evidence_number(token):
    # Do not let an unsupported digit sequence such as 六五 collapse to 五.
    if not (re.fullmatch(r'\d+(?:\.\d+)?',token) or CHINESE_NUMBER.fullmatch(token)):
        return None
    return numeric(token)


def hazard(text, operations):
    # Historical reports do not change the present reference frame. Mixed scopes
    # are conservatively retained when an explicit current clause is present.
    if HISTORY.search(text) and not re.search(r'现在|这组|本组|刚才|刚刚', text):
        return None
    kinds=[]
    if (any(o.get('intent')=='EXERCISE_SWITCH' and o.get('who','self')!='other' for o in operations)
            or (SWITCH.search(text) and any(a in text for aliases in EXERCISES.values() for a in aliases))):
        kinds.append('exercise_or_instance_change')
    if WEIGHT_UNIT.search(text) or (CHANGE.search(text) and re.search(r'\d|[一二两三四五六七八九十]',text)):
        kinds.append('weight_reference_change')
    if not kinds:
        return None
    return {'version':1,'kinds':kinds,'source_text':text,
            'unconfirmed_candidates':[o for o in operations if o.get('intent') in
                                      ('EXERCISE_SWITCH','SET_COMPLETED','CORRECTION','NEXT_SET_PLAN_CHANGED')]}


def independent(text, op, ctx, *, require_reps=True):
    """Require current, explicit exercise AND exact unit-bearing weight in text."""
    if not op.exercise or op.weight_kg is None:
        return False
    candidates=check_coverage(text,SemanticResult(operations=[]),ctx)
    if len(candidates)!=1:
        return False
    clause=candidates[0].evidence_text
    aliases=EXERCISES.get(op.exercise,())
    mixed_scope=any(p.search(text) for p in (HISTORY,PLAN,CONDITION,NEGATIVE,OTHER,QUESTION))
    if not any(a in clause or (not mixed_scope and a in text) for a in aliases):
        return False
    if SIGNED_WEIGHT.search(clause) or SIGNED_COUNT.search(clause):
        return False
    if re.search(r'\d[,，]\d{3}(?:\.\d+)?\s*(?:公斤|千克|kg)',text,re.I):
        return False
    weights=[evidence_number(m.group(1)) for m in WEIGHT.finditer(clause)]
    if weights!=[op.weight_kg]:
        return False
    counts=[evidence_number(m.group().rstrip('个次').strip()) for m in COUNT.finditer(clause)]
    return not require_reps or counts==[op.reps]


def reference_reason(text,op,ctx):
    if not REFERENCE.search(text) or independent(text,op,ctx):
        return None
    # The existing engine validates genuinely omitted weights. This gate also
    # validates values the model filled from Context as if they were explicit.
    if op.weight_kg is None:
        # Inherited weight plus a fused contextual count ("还是67个" for the
        # spoken "还是60，7个") means the count itself must not become reps.
        if (op.intent == 'SET_COMPLETED' and
                re.search(r'(?:还是|仍然)\s*' + NUM + r'\s*(?:个|次)', text)):
            return 'ASR_NUMERIC_AMBIGUITY_REQUIRES_CONFIRMATION'
        return None
    candidates=check_coverage(text,SemanticResult(operations=[]),ctx)
    weights=[evidence_number(m.group(1)) for d in candidates for m in WEIGHT.finditer(d.evidence_text)]
    if re.search(r'(?:还是|仍然)\s*'+NUM+r'\s*(?:个|次)',text) and not weights:
        return 'ASR_NUMERIC_AMBIGUITY_REQUIRES_CONFIRMATION'
    named=any(a in d.evidence_text for d in candidates for a in EXERCISES.get(op.exercise,()))
    if not named and (not ctx.active_exercise_instance_id or op.exercise not in (None,ctx.active_exercise_name)):
        return 'UNRELIABLE_CONTEXT_REFERENCE'
    if weights:
        if weights!=[op.weight_kg] or any(SIGNED_WEIGHT.search(d.evidence_text) for d in candidates):
            return 'UNRELIABLE_CONTEXT_REFERENCE'
    elif (ctx.last_reported_weight is None or op.weight_kg!=ctx.last_reported_weight or
          ctx.next_set_plan not in (None,ctx.last_reported_weight)):
        return 'UNRELIABLE_CONTEXT_REFERENCE'
    return None


def dependencies(db,sid,uid,ctx,*,exclude_pending=None):
    cursor=db.execute('SELECT MIN(event_seq) FROM session_events WHERE session_id=? AND utterance_id=?',(sid,uid)).fetchone()[0]
    cutoff=cursor if cursor is not None else db.execute('SELECT COALESCE(MAX(event_seq),0)+1 FROM session_events WHERE session_id=?',(sid,)).fetchone()[0]
    # A later independently reported formal set re-establishes the reference
    # frame. A model-filled EXPLICIT flag alone is not sufficient evidence.
    anchor=0
    rows=db.execute('SELECT s.weight_kg,s.reps,e.exercise_name,u.transcript,'
        '(SELECT MIN(event_seq) FROM session_events v WHERE v.session_id=u.session_id AND v.utterance_id=u.id) AS source_cursor '
        'FROM sets s JOIN exercise_instances e ON e.id=s.exercise_instance_id '
        'JOIN utterances u ON u.session_id=e.session_id AND u.id=s.source_utterance_id '
        "WHERE e.session_id=? AND s.is_voided=0 AND s.weight_provenance='EXPLICIT'",(sid,))
    for row in rows:
        if row['source_cursor'] and row['source_cursor']<cutoff:
            op=SemanticOperation(operation_index=0,intent='SET_COMPLETED',exercise=row['exercise_name'],weight_kg=row['weight_kg'],reps=row['reps'])
            if independent(row['transcript'],op,ctx,require_reps=False):
                anchor=max(anchor,row['source_cursor'])
    links=[]
    rows=db.execute('SELECT p.*,u.transcript,'
        'COALESCE((SELECT MIN(event_seq) FROM session_events v WHERE v.session_id=p.session_id AND v.utterance_id=p.utterance_id),0) AS source_cursor '
        'FROM pending_operations p JOIN utterances u ON u.session_id=p.session_id AND u.id=p.utterance_id '
        "WHERE p.session_id=? AND p.status='open' AND p.id!=? ORDER BY p.id",(sid,exclude_pending or -1))
    for row in rows:
        if row['utterance_id']==uid or row['source_cursor']>=cutoff:
            continue
        if row['source_cursor'] and row['source_cursor']<=anchor:
            continue
        payload=json.loads(row['payload_json'])
        evidence=hazard(row['transcript'],payload.get('operations',[]))
        if evidence:
            links.append({'pending_id':row['id'],'utterance_id':row['utterance_id'],
                          'input_event_cursor':row['source_cursor'],'reason':row['reason'],**evidence})
    return links
