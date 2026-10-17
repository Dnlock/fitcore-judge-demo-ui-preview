"""Conservative coverage diagnostics. Never converts text into executable operations."""
from dataclasses import asdict, dataclass
import re

from app.schemas import ContextRead, SemanticResult

COUNT = re.compile(r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+)\s*(?:个|次|下)')
# A decimal point belongs to the numeric token; other periods remain sentence boundaries.
CLAUSE = re.compile(r'(?:[^，,。.!！;；\n]|(?<=\d)\.(?=\d))+')
HISTORY = re.compile(r'上次|上回|昨天|前天|去年|以前|之前那次|上一组|上组')
PLAN = re.compile(r'下[一两]?组|下一次|打算|准备|计划|想试|将要|明天|待会')
CONDITION = re.compile(r'如果|假如|要是|假设')
NEGATIVE = re.compile(r'没有|没做|没完成|未做|未完成|不做|不要|不想|不是|不能')
QUESTION = re.compile(r'[?？吗]|是不是|能不能|能否|可不可以')
OTHER = re.compile(r'别人|教练|朋友|他做|她做')
CURRENT = re.compile(r'这次|这[一]?组|现在|实际|本组|刚[刚]?完成')
CORRECTION = re.compile(r'纠正|更正|改成|其实|说错|记错|不对|第[零一二两三四五六七八九十百\d]+组.*(?:实际|应为)')
TRAINING = re.compile(r'公斤|千克|kg|重量|卧推|飞鸟|深蹲|硬拉|推举|划船|引体|组|做|完成|还是')


@dataclass(frozen=True)
class CoverageDiagnostic:
    code: str
    expected_intent: str
    evidence_text: str
    start: int
    end: int
    scope: str
    observed_intents: tuple[str, ...]

    def to_dict(self):
        result = asdict(self)
        result['observed_intents'] = list(self.observed_intents)
        return result


@dataclass(frozen=True)
class SelectiveReparseRequest:
    """Future boundary only: constructing this object never calls a provider."""
    utterance_id: str
    transcript: str
    context_version: int
    diagnostics: tuple[CoverageDiagnostic, ...]
    target_intents: tuple[str, ...] = ('SET_COMPLETED',)


def check_coverage(text: str, parsed: SemanticResult, context: ContextRead) -> list[CoverageDiagnostic]:
    candidates = []
    scope, training_seen = 'current', context.active_exercise_instance_id is not None
    for match in CLAUSE.finditer(text):
        clause = match.group()
        # A correction can continue after a comma ("记错了，实际八次").
        # "实际" in that continuation must not turn it into a new completed set.
        if CURRENT.search(clause) and scope != 'correction':
            scope = 'current'
        if HISTORY.search(clause):
            scope = 'historical'
        if CONDITION.search(clause):
            scope = 'conditional'
        elif PLAN.search(clause):
            scope = 'plan'
        if NEGATIVE.search(clause):
            scope = 'negative'
        if QUESTION.search(clause):
            scope = 'question'
        if OTHER.search(clause):
            scope = 'other'
        if CORRECTION.search(clause):
            scope = 'correction'
        training_seen = training_seen or bool(TRAINING.search(clause))
        if scope != 'current' or not training_seen:
            continue
        for count in COUNT.finditer(clause):
            # Common non-training counts must not become completed-set evidence.
            if re.search(r'吃|喝|买', clause[max(0, count.start() - 6):count.start()]):
                continue
            if re.match(r'苹果|杯|瓶|口|分钟|小时', clause[count.end():]):
                continue
            candidates.append((match.start(), match.end(), clause))
    # Existing operations are only evidence of coverage, never new facts. Their
    # subject/time/modality, values and references remain the Core's responsibility.
    # Corrections also absorb a matched count ("8个，不对，是9个"): the changed
    # number is correction evidence, not a missing completion.
    covered = sum(op.intent in ('SET_COMPLETED', 'CORRECTION') for op in parsed.operations)
    return [CoverageDiagnostic('MISSING_SET_COMPLETED', 'SET_COMPLETED', clause, start, end,
                'current', tuple(op.intent for op in parsed.operations))
            for start, end, clause in candidates[covered:]]
