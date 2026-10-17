import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./fitcore.db"
    model_provider: str = "real"
    model_base_url: str = ""
    model_id: str = ""
    model_api_key: str = field(default="", repr=False)
    model_api_protocol: str = "unknown"
    model_timeout_seconds: float = 20.0
    model_enable_thinking: bool | None = None
    http_api_token: str = field(default="", repr=False)
    audio_worker_enabled: bool = False
    audio_test_source_trusted: bool = False
    audio_worker_poll_seconds: float = 0.25
    audio_worker_retry_seconds: float = 1.0
    audio_worker_max_attempts: int = 2
    numeric_recheck_enabled: bool = False

    @classmethod
    def from_env(cls):
        thinking = os.getenv("MODEL_ENABLE_THINKING", "").strip().lower()
        if thinking not in {"", "true", "false"}:
            raise ValueError("MODEL_ENABLE_THINKING must be true, false, or unset")
        return cls(
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            model_provider=os.getenv("MODEL_PROVIDER", cls.model_provider),
            model_base_url=os.getenv("MODEL_BASE_URL", ""),
            model_id=os.getenv("MODEL_ID", ""),
            model_api_key=os.getenv("MODEL_API_KEY", ""),
            model_api_protocol=os.getenv("MODEL_API_PROTOCOL", "unknown"),
            model_timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "20")),
            model_enable_thinking=None if not thinking else thinking == "true",
            http_api_token=os.getenv('FITCORE_HTTP_API_TOKEN', ''),
            audio_worker_enabled=os.getenv('AUDIO_WORKER_ENABLED', 'false').lower() == 'true',
            audio_test_source_trusted=os.getenv('AUDIO_TEST_SOURCE_TRUSTED', 'false').lower() == 'true',
            audio_worker_poll_seconds=float(os.getenv('AUDIO_WORKER_POLL_SECONDS', '0.25')),
            audio_worker_retry_seconds=float(os.getenv('AUDIO_WORKER_RETRY_SECONDS', '1')),
            audio_worker_max_attempts=int(os.getenv('AUDIO_WORKER_MAX_ATTEMPTS', '2')),
            numeric_recheck_enabled=os.getenv('NUMERIC_RECHECK_ENABLED', 'false').lower() == 'true',
        )
