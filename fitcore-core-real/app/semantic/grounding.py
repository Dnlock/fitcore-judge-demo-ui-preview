"""Shared numeric commit evidence and stricter selective-reparse grounding."""
import re
from decimal import Decimal, InvalidOperation
from app.semantic.coverage import (COUNT, PLAN, CLAUSE, HISTORY, CONDITION,
                                   NEGATIVE, QUESTION, OTHER, CURRENT, check_coverage)
from app.correction import current_correction
from app.schemas import SemanticOperation, SemanticResult

NUM = r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+)'
WEIGHT = re.compile(r'(?<![\d零〇一二两三四五六七八九十百千万点.eE＋+\-−－．])('
                    + NUM + r')\s*(?:公斤|千克|kg)(?![A-Za-z])', re.I)
SIGNED_WEIGHT = re.compile(r'[+\-−＋－]\s*' + NUM + r'\s*(?:公斤|千克|kg)', re.I)
WEIGHT_UNIT = re.compile('(?:'+NUM+r')\s*(?:公斤|千克|克|斤|磅|kg|lbs?|g)',re.I)
SIGNED_COUNT = re.compile(r'[+\-−＋－]\s*'+NUM+r'\s*(?:个|次)')
# Progressive/range loads ("60到65公斤") are not a completed-set weight: the
# model may pick one endpoint while the utterance names a span, so committing
# either value from this clause alone is speculation.
RANGE_WEIGHT = re.compile(NUM + r'\s*(?:到|至)\s*' + NUM + r'\s*(?:公斤|千克|kg)', re.I)
# Rep ranges ("做8到10个") are a plan/estimate span, never an exact completed
# count; picking either endpoint would fabricate a fact.
RANGE_COUNT = re.compile(NUM + r'\s*(?:到|至)\s*' + NUM + r'\s*(?:个|次)')
# Reps-in-reserve / capacity utterances ("还能做两个", "顶多再三个",
# "还有两次余力", "再两个就到力竭了") are subjective capacity, never a
# completed set; their number must never become a completed rep count.
# A plain '再做N个' with a reference anchor ("这个重量再做11个") stays a
# completion: the capacity heads never appear there.
RIR_COUNT = re.compile(
    r'(?:还能|还可以|最多|至多|顶多)\s*[能做再来]*\s*' + NUM + r'\s*(?:个|次)'
    r'|(?:还有|只剩|剩)\s*' + NUM + r'\s*(?:个|次)\s*余力'
    r'|再\s*' + NUM + r'\s*(?:个|次)\s*[^，。]{0,8}力竭')
# Approximate counts ("大约八个", "做了八个半", "8个左右") are not exact
# evidence; the clean number inside them must not round into an exact fact.
APPROX_COUNT = re.compile(
    r'(?:大约|大概|差不多|将近|约)\s*(?:做了|做|有|是)?\s*' + NUM + r'\s*(?:个|次)'
    r'|' + NUM + r'\s*(?:个|次)\s*(?:左右|上下)'
    r'|' + NUM + r'\s*个半')
# Increment expressions ("刚才又做了两个", "再多做了两个") name a delta on
# top of an existing set, not a standalone completed set of that count. The
# bare "再做11个" reference completion has no 了/补 counterpart and stays valid.
INCREMENT_COUNT = re.compile(
    r'(?:刚才|刚刚)?\s*(?:又|再|多)\s*(?:做了|补了|加了|完成了?|干了)'
    r'\s*(' + NUM + r')\s*(?:个|次)')
# A same-frame completion anchor ("重量不变，又完成了4次", "还是这个重量，又
# 做了4个") re-establishes inheritance: the count is a completed set, not a
# floating delta. Only anchor-free deltas are conservative defers.
INCREMENT_ANCHOR = re.compile(r'重量不变|照旧|一样|同样|相同|这个重量|这重量|原重量|还是|仍然')
EXERCISES = {'bench_press': ('卧推',), 'dumbbell_fly': ('飞鸟',), 'squat': ('深蹲',)}
# Spoken action nouns recognized by the text-evidence layer (known aliases plus
# common training-motion words). When text names an action and NO claimed
# exercise's alias appears in it, the provider's exercise binding contradicts
# the utterance and nothing may commit.
ACTION_NOUNS = ('卧推', '飞鸟', '深蹲', '硬拉', '划船', '推举', '引体')
SWITCH_MARKER = re.compile(r'换|切换|改做|开始|接着做')


def derive_switch_completion(text, context, parsed):
    """Deterministic completion evidence for a switch-and-complete utterance.

    When the utterance is a single switch op that already carries a weight and
    a count inside the model's payload, and the text itself contains exactly
    one switch marker, one exercise alias, one unit-bearing weight and one
    current-scope count that all agree, the completion is text-proven: return
    the switch plus a SET_COMPLETED carrying the same values. Any missing or
    conflicting evidence returns None and the normal conservative path applies.
    Never invents values and never fires for multi-op or ambiguous answers.
    """
    ops = parsed.operations
    if len(ops) != 1 or parsed.ambiguities:
        return None
    op = ops[0]
    if (op.intent != 'EXERCISE_SWITCH' or op.when != 'current' or op.who != 'self' or
            op.exercise is None or op.weight_kg is None or op.reps is None):
        return None
    if not SWITCH_MARKER.search(text):
        return None
    aliases = EXERCISES.get(op.exercise, ())
    if not any(alias in text for alias in aliases):
        return None
    candidates = check_coverage(text, SemanticResult(operations=[]), context)
    if len(candidates) != 1:
        return None
    clause = candidates[0].evidence_text
    if PLAN.search(clause) or NEGATIVE.search(clause) or QUESTION.search(clause) or OTHER.search(clause):
        return None
    if SIGNED_WEIGHT.search(clause) or SIGNED_COUNT.search(clause):
        return None
    from app.context_safety import evidence_number
    weights = [evidence_number(m.group(1)) for m in WEIGHT.finditer(clause)]
    counts = [evidence_number(c.group().rstrip('个次下').strip()) for c in COUNT.finditer(clause)]
    if len(weights) != 1 or len(counts) != 1 or None in weights or None in counts:
        return None
    if weights[0] != op.weight_kg or counts[0] != op.reps:
        return None
    completed = SemanticOperation(operation_index=1, intent='SET_COMPLETED', who='self',
                                  when='current', modality='fact', exercise=op.exercise,
                                  weight_kg=op.weight_kg, reps=op.reps)
    return SemanticResult(operations=[op, completed], ambiguities=parsed.ambiguities)


def decimal_numeric(token):
    try:
        return Decimal(token)
    except InvalidOperation:
        # Reuse the existing canonical Chinese numeral validator. Import at call
        # time because context_safety itself imports the shared token patterns.
        from app.context_safety import evidence_number
        value=evidence_number(token)
        return Decimal(str(value)) if value is not None else None


def verify_numeric_evidence(text, context, proposed):
    """Shared pre-commit numeric gate. Diagnoses evidence, never fills slots.

    Unspecified units remain legacy kg inputs. Explicit unsupported units/scopes
    cannot be erased by a provider, nor borrowed from a future/historical clause.
    Context reliability and correction target identity remain Core responsibilities.
    """
    operations=[o for o in proposed.operations if o.intent in ('SET_COMPLETED','CORRECTION')
                and o.who=='self' and o.when=='current' and o.modality=='fact']
    if not operations:return None
    # If the utterance names a training action, at least one claimed exercise
    # alias must appear in the text; otherwise the exercise binding is a guess
    # that contradicts the spoken action ("硬拉60公斤8个" labeled bench_press).
    # A pure correction carries no claimed exercise and binds the active frame,
    # so it is exempt ("卧推60公斤8个，不对，是9个").
    spoken_actions = {a for a in ACTION_NOUNS if a in text}
    claimed = {o.exercise for o in proposed.operations if o.exercise}
    if spoken_actions and claimed and not any(alias in text for exercise in claimed
                                              for alias in EXERCISES.get(exercise, ())):
        return 'EXERCISE_TEXT_MISMATCH'
    # Multi-action utterances: each claimed exercise must be provable from the
    # clause that names it. When the model swaps actions between clauses (or
    # moves numbers across them) the digits no longer sit in the named clause.
    if len(spoken_actions) >= 2:
        for op in operations:
            if not op.exercise:
                continue
            aliases = EXERCISES.get(op.exercise, ())
            digits = {Decimal(str(op.weight_kg)) if op.weight_kg is not None else None,
                      Decimal(str(op.reps)) if op.reps is not None else None}
            digits.discard(None)
            if not digits:
                continue
            named_clause_digits = []
            for match in CLAUSE.finditer(text):
                clause = match.group()
                if not any(alias in clause for alias in aliases):
                    continue
                named_clause_digits.append({decimal_numeric(t) for t in re.findall(NUM, clause)} - {None})
            # A named clause without digits defers to the inheritance paths;
            # a named clause WITH digits must contain this op's own digits.
            if named_clause_digits and any(not (digits <= clause_digits)
                                           for clause_digits in named_clause_digits):
                return 'EXERCISE_TEXT_MISMATCH'
    # A number alone supplies no completion or reference evidence. A provider
    # filling exercise/weight slots cannot turn it into a completed set. Keep
    # this in the shared commit gate so initial and resumed candidates agree.
    if (any(o.intent=='SET_COMPLETED' for o in operations) and
        re.fullmatch(r'[\s，,。.!！?？、；;：:]*[+\-−＋－]?\s*'
                     r'(?:\d+(?:[.．]\d+)?|[零〇一二两三四五六七八九十百千万点]+)'
                     r'[\s，,。.!！?？、；;：:]*', text)):
        return 'INSUFFICIENT_COMPLETION_EVIDENCE'
    scope='current';current=[];noncurrent=[]
    only_corrections = bool(operations) and all(o.intent == 'CORRECTION' for o in operations)
    negated = set()
    for match in CLAUSE.finditer(text):
        clause=match.group()
        if CURRENT.search(clause) or re.search('当前|目前',clause):scope='current'
        if HISTORY.search(clause):scope='historical'
        if CONDITION.search(clause):scope='conditional'
        elif PLAN.search(clause):scope='plan'
        # Corrections such as "不是7个，是8个" are checked by target validation;
        # don't treat their negative old value as evidence for a completed set.
        # For a pure correction utterance the negated weight ("不是60公斤，是65
        # 公斤") is the OLD value: it must not participate in weight ambiguity.
        if NEGATIVE.search(clause):
            if only_corrections:
                negated |= {w for w in (decimal_numeric(m.group(1)) for m in WEIGHT.finditer(clause))
                            if w is not None}
            else:
                scope='negative'
        if QUESTION.search(clause) or OTHER.search(clause):scope='other'
        (current if scope=='current' else noncurrent).append(clause)
    current_text='，'.join(current)
    if RANGE_COUNT.search(current_text) and any(o.intent == 'SET_COMPLETED' for o in operations):
        return 'NUMERIC_EVIDENCE_RANGE_COUNT_UNCONFIRMED'
    if RIR_COUNT.search(current_text) and any(o.intent == 'SET_COMPLETED' for o in operations):
        return 'NUMERIC_EVIDENCE_NON_COMPLETED_COUNT'
    if WEIGHT_UNIT.search(current_text):
        if (SIGNED_WEIGHT.search(current_text) or
            re.search(r'\d[,，]\d{3}(?:\.\d+)?\s*(?:公斤|千克|kg)',text,re.I)):
            return 'NUMERIC_EVIDENCE_UNSUPPORTED_WEIGHT'
        if RANGE_WEIGHT.search(text):
            return 'NUMERIC_EVIDENCE_AMBIGUOUS_WEIGHT_BINDING'
        if re.search(r'每只|每手|单手|每侧|每边|额外|自重|(?:公斤|千克|kg)\s*/\s*(?:只|手|侧|边)|各\s*'+NUM,current_text,re.I):
            return 'NUMERIC_EVIDENCE_UNSUPPORTED_LOAD_SCOPE'
        # Every explicit unit must be a whole supported kg expression. An lb
        # token alongside a kg token is not silently ignored.
        units=list(WEIGHT_UNIT.finditer(current_text));matches=list(WEIGHT.finditer(current_text))
        if len(units)!=len(matches):return 'NUMERIC_EVIDENCE_UNSUPPORTED_UNIT_OR_NUMBER'
        weights=[decimal_numeric(m.group(1)) for m in matches]
        if None in weights:return 'NUMERIC_EVIDENCE_UNSUPPORTED_UNIT_OR_NUMBER'
        live_weights=[w for w in weights if w not in negated]
        if len(set(live_weights))>1 or (negated and not live_weights):
            return 'NUMERIC_EVIDENCE_AMBIGUOUS_WEIGHT_BINDING'
        for op in operations:
            if op.weight_kg is not None and Decimal(str(op.weight_kg)) not in live_weights:
                return 'NUMERIC_EVIDENCE_WEIGHT_MISMATCH'
            if op.intent=='SET_COMPLETED' and op.weight_kg is None:
                if context.last_reported_weight is None or Decimal(str(context.last_reported_weight)) not in weights:
                    return 'NUMERIC_EVIDENCE_EXPLICIT_WEIGHT_NOT_APPLIED'
    elif any(WEIGHT_UNIT.search(c) for c in noncurrent):
        # A current completion cannot use the only explicit weight from a plan.
        if any(o.intent=='SET_COMPLETED' and o.weight_kg not in (None,context.last_reported_weight) for o in operations):
            return 'NUMERIC_EVIDENCE_WEIGHT_ROLE_CONFLICT'
        if not COUNT.search(current_text) and any(o.intent=='SET_COMPLETED' for o in operations):
            return 'NUMERIC_EVIDENCE_NO_CURRENT_FACT'
    if any(o.intent=='SET_COMPLETED' for o in operations) and not COUNT.search(current_text):
        # A completion needs its own count evidence in the current scope;
        # nothing may be invented for count-less references like "还是这个重量。"
        return 'NUMERIC_EVIDENCE_NO_CURRENT_FACT'
    # Reps/count consistency (shared with the reparse gate's ungrounded-reps
    # rule): a proposed rep count must appear among the current-scope spoken
    # counts. This closes the initial-parse hole where the model could inflate
    # "8个" into 9 and still commit a completed set.
    count_matches = list(COUNT.finditer(current_text))
    approx_spans = [m.span() for m in APPROX_COUNT.finditer(current_text)]
    count_values, approx_values, exact_values = [], set(), []
    for match in count_matches:
        value = decimal_numeric(match.group().rstrip('个次下').strip())
        if value is None:
            continue
        count_values.append(value)
        if any(start <= match.start() and match.end() <= end for start, end in approx_spans):
            approx_values.add(value)
        else:
            exact_values.append(value)
    if count_values:
        for op in operations:
            if op.reps is not None and Decimal(str(op.reps)) not in count_values:
                return 'NUMERIC_EVIDENCE_REPS_MISMATCH'
    if any(o.intent == 'SET_COMPLETED' and o.reps is not None for o in operations):
        proposed = {Decimal(str(o.reps)) for o in operations if o.reps is not None}
        if (proposed & approx_values) and not (proposed & set(exact_values)):
            # The only count evidence for this value sits inside an approximate
            # expression ("大约八个"): rounding it to an exact fact is unsafe.
            return 'NUMERIC_EVIDENCE_APPROXIMATE_COUNT'
        inc_values = {decimal_numeric(m.group(1)) for m in INCREMENT_COUNT.finditer(current_text)}
        inc_values.discard(None)
        if (proposed & inc_values) and not INCREMENT_ANCHOR.search(current_text):
            # A delta on top of an existing set ("又做了两个") is never a
            # standalone completed set of that count — unless a same-frame
            # anchor ("重量不变，又完成了4次") re-establishes inheritance.
            return 'NUMERIC_EVIDENCE_INCREMENT_COUNT'
    if not WEIGHT_UNIT.search(text) and any(o.intent=='SET_COMPLETED' and o.weight_kg is not None
                                            for o in operations):
        from app.context_safety import REFERENCE
        if not REFERENCE.search(text):
            # A non-reference utterance with a unitless explicit weight must be
            # traceable: either a literal number in the utterance ("60，7个")
            # or a verified context anchor (last reported weight, active
            # next-set plan). A fused ASR number such as "67" misread from
            # "60，7个" matches neither and must be confirmed. Reference
            # utterances keep the more specific unreliable-reference gate.
            # Only the last REPORTED (executed) weight anchors a unitless
            # weight; a next-set plan is a future intent and can never prove
            # that a completed set was performed at that weight.
            present = {Decimal(str(n)) for n in
                       (decimal_numeric(t) for t in re.findall(r'\d+(?:\.\d+)?', current_text))
                       if n is not None}
            if context.last_reported_weight is not None:
                present.add(Decimal(str(context.last_reported_weight)))
            for op in operations:
                if op.intent=='SET_COMPLETED' and op.weight_kg is not None and \
                        Decimal(str(op.weight_kg)) not in present:
                    return 'NUMERIC_EVIDENCE_WEIGHT_NOT_IN_TEXT'
    return None


def numeric(token):
    try:
        return float(token)
    except ValueError:
        digits = dict(zip('零〇一二两三四五六七八九', [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
        total, current = 0, 0
        for char in token:
            if char in digits:
                current = digits[char]
            elif char in '十百':
                total += (current or 1) * (10 if char == '十' else 100)
                current = 0
            else:
                return None
        return total + current


def verify_grounding(text, context, original, proposed):
    """Return a fixed reason on unsupported evidence. Only recovery uses this gate."""
    candidates = check_coverage(text, SemanticResult(operations=[]), context)
    if len(candidates) > 1:
        return 'REPARSE_MULTIPLE_SET_BINDINGS_REQUIRE_CONFIRMATION'
    reps = [numeric(COUNT.search(d.evidence_text).group().rstrip('个次').strip()) for d in candidates]
    weights = [numeric(m.group(1)) for d in candidates for m in WEIGHT.finditer(d.evidence_text)]
    # Unsupported signs, grouping and units must not lose their prefix and then
    # masquerade as a positive weight or fall back to an unrelated Context value.
    if any(SIGNED_WEIGHT.search(d.evidence_text) for d in candidates):
        return 'REPARSE_UNSUPPORTED_SIGNED_WEIGHT'
    if any(SIGNED_COUNT.search(d.evidence_text) for d in candidates):
        return 'REPARSE_UNSUPPORTED_SIGNED_REPS'
    if re.search(r'\d[,，]\d{3}(?:\.\d+)?\s*(?:公斤|千克|kg)',text,re.I):
        return 'REPARSE_UNSUPPORTED_GROUPED_WEIGHT'
    if any(WEIGHT_UNIT.search(d.evidence_text) for d in candidates) and not weights:
        return 'REPARSE_UNSUPPORTED_WEIGHT_EXPRESSION'
    if len(set(weights))>1:
        return 'REPARSE_MULTIPLE_WEIGHT_BINDINGS_REQUIRE_CONFIRMATION'
    if re.search(r'(?:还是|仍然)\s*' + NUM + r'\s*(?:个|次)', text) and not weights:
        return 'ASR_NUMERIC_AMBIGUITY_REQUIRES_CONFIRMATION'
    original_intents = {op.intent for op in original.operations}
    for old in original.operations:
        if (old.intent == 'EXERCISE_SWITCH' and old.exercise and
            any(alias in text for alias in EXERCISES.get(old.exercise, ())) and
            re.search(r'换|切换|改做|开始|接着做', text) and
            not any(op.intent == 'EXERCISE_SWITCH' and op.exercise == old.exercise for op in proposed.operations)):
            return 'REPARSE_MISSING_EXERCISE_SWITCH'
    # Text-grounded original intents must survive the selective reparse; a
    # re-answer that silently drops a note, correction or plan would erase a
    # user fact the initial parse had already captured. Single-intent originals
    # may legitimately be re-answered as another intent when the whole parse
    # was wrong; multi-intent originals must keep every grounded sibling.
    if len(original.operations) >= 2:
        for old in original.operations:
            present = any(op.intent == old.intent and
                          op.model_dump(exclude={'operation_index'}) == old.model_dump(exclude={'operation_index'})
                          for op in proposed.operations)
            if old.intent == 'TRAINING_NOTE_ADDED' and old.note and old.note in text and not present:
                return 'REPARSE_MISSING_NOTE'
            if old.intent == 'CORRECTION' and current_correction(text) and not present:
                return 'REPARSE_MISSING_CORRECTION'
            if old.intent == 'NEXT_SET_PLAN_CHANGED' and PLAN.search(text) and not present:
                return 'REPARSE_MISSING_PLAN'
    current_exercise = context.active_exercise_name
    switched = False
    for op in proposed.operations:
        if op.intent not in original_intents | {'SET_COMPLETED', 'EXERCISE_SWITCH'}:
            return 'REPARSE_UNSUPPORTED_NEW_INTENT'
        if op.intent == 'EXERCISE_SWITCH':
            if not op.exercise or not any(alias in text for alias in EXERCISES.get(op.exercise, ())):
                return 'REPARSE_UNGROUNDED_EXERCISE'
            if not re.search(r'换|切换|改做|开始|接着做', text):
                return 'REPARSE_UNGROUNDED_SWITCH'
            current_exercise, switched = op.exercise, True
        elif op.intent == 'SET_COMPLETED':
            if op.when != 'current' or op.modality != 'fact' or op.who != 'self' or op.reps not in reps:
                return 'REPARSE_UNGROUNDED_REPS'
            reps.remove(op.reps)
            if op.exercise and op.exercise != current_exercise:
                return 'REPARSE_SWITCH_ORDER_OR_EXERCISE'
            if not (op.exercise or current_exercise):
                return 'REPARSE_UNGROUNDED_EXERCISE'
            if op.weight_kg not in weights:
                if weights:
                    return 'REPARSE_UNGROUNDED_WEIGHT'
                if (switched or context.last_reported_weight is None or
                    op.weight_kg not in (None, context.last_reported_weight) or
                    context.next_set_plan not in (None, context.last_reported_weight) or
                    not re.search(r'同样|这个重量|这重量|原重量|相同|还是', text)):
                    return 'REPARSE_UNGROUNDED_WEIGHT'
        elif op.intent == 'TRAINING_NOTE_ADDED':
            if not op.note or op.note not in text:
                return 'REPARSE_UNGROUNDED_NOTE'
        elif op.intent == 'CORRECTION':
            if not any(old.intent == 'CORRECTION' and old.model_dump(exclude={'operation_index'}) ==
                       op.model_dump(exclude={'operation_index'}) for old in original.operations):
                return 'REPARSE_CORRECTION_CHANGE_REQUIRES_CONFIRMATION'
        elif op.intent == 'NEXT_SET_PLAN_CHANGED':
            if not PLAN.search(text):
                return 'REPARSE_UNGROUNDED_PLAN'
            if not any(old.intent == op.intent and old.model_dump(exclude={'operation_index'}) ==
                       op.model_dump(exclude={'operation_index'}) for old in original.operations):
                return 'REPARSE_PLAN_CHANGE_REQUIRES_CONFIRMATION'
    return verify_numeric_evidence(text, context, proposed)
