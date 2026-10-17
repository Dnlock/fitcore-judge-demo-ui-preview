from typing import Protocol
from app.schemas import ContextRead, SemanticResult


class SemanticProvider(Protocol):
    def parse(self, transcript: str, context: ContextRead) -> SemanticResult: ...
