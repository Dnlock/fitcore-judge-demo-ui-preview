"""Deterministic fixtures only. Never use to claim language-model accuracy."""
from app.schemas import ContextRead, SemanticOperation as Op, SemanticResult


class FakeSemanticProvider:
    def parse(self, transcript: str, context: ContextRead) -> SemanticResult:
        normalized = transcript.strip().replace("，", ",").replace("。", "")
        def result(*ops, ambiguities=None):
            return SemanticResult(operations=[Op(operation_index=i, **value) for i, value in enumerate(ops)],
                                  ambiguities=ambiguities or [])
        fixtures = {
            "卧推60公斤第一组8个": [dict(intent="SET_COMPLETED", exercise="bench_press", weight_kg=60, reps=8, set_number=1)],
            "卧推60公斤8个": [dict(intent="SET_COMPLETED", exercise="bench_press", weight_kg=60, reps=8)],
            "同样重量7个,有点重,下一组减5公斤": [
                dict(intent="SET_COMPLETED", reps=7),
                dict(intent="TRAINING_NOTE_ADDED", note="有点重"),
                dict(intent="NEXT_SET_PLAN_CHANGED", weight_delta_kg=-5, when="future", modality="plan")],
            "换飞鸟5公斤第一组12个": [dict(intent="EXERCISE_SWITCH", exercise="dumbbell_fly"),
                                          dict(intent="SET_COMPLETED", weight_kg=5, reps=12, set_number=1)],
            "还是这个重量11个": [dict(intent="SET_COMPLETED", reps=11)],
            "同样重量8个": [dict(intent="SET_COMPLETED", reps=8)],
            "刚才第二组其实8个": [dict(intent="CORRECTION", set_number=2, reps=8)],
            "刚才第一组其实是50公斤": [dict(intent="CORRECTION", set_number=1, weight_kg=50)],
            "下一组想试70公斤": [dict(intent="NEXT_SET_PLAN_CHANGED", weight_kg=70, when="future", modality="plan")],
            "今天到这里": [dict(intent="SESSION_END")],
            "上次我60做了8个": [dict(intent="SET_COMPLETED", weight_kg=60, reps=8, when="historical")],
            "晚上吃什么": [],
            "如果下一组减5公斤": [dict(intent="NEXT_SET_PLAN_CHANGED", weight_delta_kg=-5,
                                             when="future", modality="hypothetical")],
            "8": [],
        }
        if normalized not in fixtures:
            return result(ambiguities=["FAKE_FIXTURE_UNSUPPORTED"])
        if normalized == "8":
            return result(ambiguities=["BARE_NUMBER"])
        return result(*fixtures[normalized])
