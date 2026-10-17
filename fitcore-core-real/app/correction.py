"""Explicit spoken set ordinals constrain model-proposed correction targets."""
import re

from app.schemas import SemanticResult
from app.semantic.coverage import check_coverage

REPAIR = re.compile(r'其实|实际|更正|纠正|改成|应为|应是|说错|记错|不是.{0,16}是')
RECENT = re.compile(r'刚才|刚刚|刚做|刚完成|上一组')
NON_CURRENT = re.compile(r'上次|上回|昨天|前天|去年|以前|下[一两]?组|下一次|准备|打算|计划|应该能|应当能|可能|预计|如果|假如|要是|[?？吗]')
UNCLEAR_EXERCISE = re.compile(r'(?:不知道|不确定|不清楚|记不清|忘了).{0,16}(?:动作|项目)')
# Missed-set supplement markers: the user adds a forgotten set instead of
# correcting an existing one. Without these markers a correction never creates
# a new set.
ADD_MARKER = re.compile(r'漏记|补录|补一组|漏了一组|忘(?:记|了)一组|还有一组|另外一组|'
                        '多做了一组|又做了一组|再做了一组|少记了|忘了记')

_NUM = r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+)'
# A negated weight is unambiguous old-value evidence. A rep count is old-value
# evidence only when tied to a set reference ("那个8个其实…", "那组不是8个");
# a bare "不是7次，是6次" carries no set reference and keeps the established
# recency/unique-path binding rules.
_OLD_WEIGHT = re.compile(r'(?:不是|没有)\s*(' + _NUM + r')\s*(?:公斤|千克|kg)')
_OLD_REPS = re.compile(
    r'(?:那个|这组|那组|该组)[^，。]{0,10}?(' + _NUM + r')\s*(?:个|次)'
    r'\s*(?:其实|实际|记成|记错|误记|改成)'
    r'|(?:那个|这组|那组|该组)[^，。]{0,10}?(?:不是|没有|记成|记错成)[了]?\s*'
    r'(' + _NUM + r')\s*(?:个|次)')


def correction_old_value(text):
    """Return ('reps'|'weight', int) for a spoken old value, or None.

    Only values tied to a correction are extracted: negated weights and
    set-referenced counts. Used to check target consistency before binding."""
    match = _OLD_WEIGHT.search(text)
    if match:
        from app.context_safety import evidence_number
        value = evidence_number(match.group(1))
        return ('weight', value) if value is not None else None
    match = _OLD_REPS.search(text)
    if match:
        from app.context_safety import evidence_number
        token = match.group(1) or match.group(2)
        value = evidence_number(token)
        return ('reps', value) if value is not None else None
    return None


def current_correction(text):
    """Evidence of correction, never an operation or a source of new numbers.

    Scope markers in trailing plan/history clauses must not turn a current
    correction into a non-current one; the leading clause window is used.
    """
    home = '，'.join(_correction_clauses(text))
    return bool(REPAIR.search(home) and not NON_CURRENT.search(home) and
                (RECENT.search(home) or explicit_set_numbers(home)))


def _correction_clauses(text):
    """Leading clauses up to the first future/history clause.

    Repair markers may span a comma ("不是8个，是10个"), so the correction's
    home is the leading clause window. Scope markers ('下一组', '上次', ...)
    that start a NEW clause must not reject the correction; a leading clause
    that itself carries such markers must.
    """
    from app.semantic.coverage import CLAUSE, HISTORY, PLAN, CONDITION, QUESTION
    homes = []
    for match in CLAUSE.finditer(text):
        clause = match.group()
        if homes and (HISTORY.search(clause) or PLAN.search(clause) or
                      CONDITION.search(clause) or QUESTION.search(clause)):
            break
        homes.append(clause)
    return homes


def semantics_conflict(text, op, ctx):
    if op.intent == 'SET_COMPLETED' and current_correction(text):
        # A separate current-scope clause with its own count keeps the
        # completion grounded ("...第二组8个，第一组其实是9个"). Only when no
        # completion evidence survives is the parsed set a misread correction.
        if not check_coverage(text, SemanticResult(operations=[]), ctx):
            return 'CORRECTION_INTENT_REQUIRED'
    if op.intent != 'CORRECTION':
        return None
    # Scope markers ('下一组', '上次', ...) in OTHER clauses must not reject a
    # correction whose own clause is current scoped.
    homes = _correction_clauses(text)
    check_text = '，'.join(homes) if homes else text
    if op.when != 'current' or op.modality != 'fact' or NON_CURRENT.search(check_text):
        return 'CORRECTION_REQUIRES_CURRENT_FACT'
    if UNCLEAR_EXERCISE.search(text):
        return 'AMBIGUOUS_CORRECTION_TARGET'
    return None


def explicit_set_numbers(text: str) -> set[int]:
    values = set()
    digits = dict(zip('零一二三四五六七八九', range(10)))
    digits['两'] = 2
    for token in re.findall(r'第([\d零一二两三四五六七八九十]+)组', text):
        if token.isdecimal():
            values.add(int(token))
        elif '十' in token:
            left, right = token.split('十', 1)
            if (not left or left in digits) and (not right or right in digits):
                values.add(digits.get(left, 1) * 10 + digits.get(right, 0))
        elif token in digits:
            values.add(digits[token])
    return values


def correction_set_numbers(text: str) -> set[int]:
    """Ordinals from correction-scope clauses only.

    In a mixed utterance such as "卧推60公斤第二组8个，第一组其实是9个。" the
    "第二组" belongs to the completion clause, not to the correction: aggregation
    across the whole text would make every mixed correction look ambiguous.
    """
    from app.semantic.coverage import CLAUSE, CORRECTION, CURRENT
    values = set()
    scope = 'current'
    for match in CLAUSE.finditer(text):
        clause = match.group()
        if CURRENT.search(clause) and scope != 'correction':
            scope = 'current'
        if CORRECTION.search(clause):
            scope = 'correction'
        if scope == 'correction':
            values |= explicit_set_numbers(clause)
    return values
