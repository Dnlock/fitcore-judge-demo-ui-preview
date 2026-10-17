from pathlib import Path
from typing import Protocol
from pydantic import BaseModel


class ASRResult(BaseModel):
    transcript: str
    audio_duration_seconds: float
    provider_duration_seconds: float | None = None
    elapsed_ms: int
    http_status: int


class ASRProvider(Protocol):
    def transcribe_file(self, path: Path) -> ASRResult: ...
