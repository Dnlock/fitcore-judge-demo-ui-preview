from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class SpeakerDecision(StrEnum):
    VERIFIED_USER = "VERIFIED_USER"
    NON_TARGET = "NON_TARGET"
    UNCERTAIN = "UNCERTAIN"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SemanticOperation(StrictModel):
    operation_index: int = Field(ge=0)
    intent: Literal["SET_COMPLETED", "TRAINING_NOTE_ADDED", "NEXT_SET_PLAN_CHANGED",
                    "CORRECTION", "EXERCISE_SWITCH", "SESSION_END"]
    who: Literal["self", "other", "unknown"] = "self"
    when: Literal["current", "historical", "future", "uncertain"] = "current"
    modality: Literal["fact", "plan", "hypothetical", "negative", "question"] = "fact"
    exercise: str | None = None
    weight_kg: float | None = None
    weight_delta_kg: float | None = None
    reps: int | None = None
    set_number: int | None = None
    note: str | None = None
    target_set_id: int | None = None
    exercise_instance_id: int | None = Field(default=None, ge=1, description='仅用于核对纠错目标训练实例，不能创建实例')


class SemanticResult(StrictModel):
    operations: list[SemanticOperation]
    ambiguities: list[str] = Field(default_factory=list)


class SessionCreate(StrictModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=120)


class ContextRead(StrictModel):
    session_id: str
    version: int
    status: str
    active_exercise_instance_id: int | None
    active_exercise_name: str | None = None
    last_completed_set_id: int | None
    last_reported_weight: float | None
    next_set_plan: float | None


class UtteranceIn(StrictModel):
    utterance_id: str = Field(min_length=1, max_length=120)
    source_type: Literal["trusted_test_text", "authenticated_audio"]
    speaker_status: SpeakerDecision
    transcript: str = Field(min_length=1, max_length=2000)
    context_version: int | None = Field(default=None, ge=0)


class UtteranceOut(StrictModel):
    utterance_id: str
    decision: Literal["COMMIT", "DEFER", "REJECT", "ERROR", "DUPLICATE", "STALE_CONTEXT", "ID_CONFLICT"]
    context_version: int
    committed: list[dict] = Field(default_factory=list)
    reason: str | None = None
    semantic_diagnostics: list[dict] = Field(default_factory=list)
    reparse: dict | None = None


class FactRead(StrictModel):
    id: int
    exercise_instance_id: int
    exercise: str
    set_number: int
    weight_kg: float
    reps: int
    weight_provenance: str
    weight_source_fact_id: int | None
    source_utterance_id: str


class PendingRead(StrictModel):
    id: int
    utterance_id: str
    reason: str
    status: str
    context_version_at_defer: int | None = None
    exercise_instance_id: int | None = None
    last_completed_set_id: int | None = None
    semantic_diagnostics: list[dict] = Field(default_factory=list)
    context_hazard: dict | None = None
    upstream_context_dependencies: list[dict] = Field(default_factory=list)


class PendingResolutionIn(StrictModel):
    resolution_id: str = Field(min_length=1, max_length=120)
    action: Literal["dismiss", "confirm_set"]
    context_version: int = Field(ge=0)
    weight_kg: float | None = None
    reps: int | None = None


class PendingResolutionOut(StrictModel):
    pending_id: int
    resolution_id: str
    decision: Literal["RESOLVED", "DISMISSED", "DUPLICATE"]
    context_version: int
    committed: list[dict] = Field(default_factory=list)


class ReviewRead(StrictModel):
    session_id: str
    context_version: int
    total_sets: int
    total_reps: int
    volume_kg_reps: float
    pending_count: int
    facts: list[FactRead]
