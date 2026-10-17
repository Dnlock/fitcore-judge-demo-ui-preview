"""Measured offline-file integration into the existing Core; no queue or automatic retries."""
import time

from app import service
from app.schemas import UtteranceIn


class TimedSemanticProvider:
    def __init__(self, provider):
        self.provider = provider
        self.elapsed_ms = 0.0

    def parse(self, transcript, context):
        started = time.perf_counter()
        try:
            return self.provider.parse(transcript, context)
        finally:
            self.elapsed_ms += (time.perf_counter() - started) * 1000


def process_audio(db, sid, uid, wav_path, asr, semantic):
    """Caller retains uid across retries. Source is a user-authorized local test only.

    ASR exceptions stop before Core. Core already rolls back on LLM failures.
    A production queue must freeze final text before retrying only the LLM stage.
    """
    started = time.perf_counter()
    result = asr.transcribe_file(wav_path)
    provider = TimedSemanticProvider(semantic)
    core_started = time.perf_counter()
    outcome = service.process(db, provider, sid, UtteranceIn(
        utterance_id=uid, source_type='trusted_test_text', speaker_status='VERIFIED_USER',
        transcript=result.transcript))
    core_total = (time.perf_counter() - core_started) * 1000
    return {
        'asr': result.model_dump(), 'outcome': outcome.model_dump(),
        'timings': {
            'audio_duration_seconds': result.audio_duration_seconds,
            'client_upload_ms': None,  # Local file runner is not an iOS upload measurement.
            'upstream_body_send_ms': asr.diagnostics.get('upstream_body_send_ms'),
            'asr_request_ms': result.elapsed_ms,
            'llm_parse_ms': round(provider.elapsed_ms, 2),
            # Includes Context reads, schema/domain validation and SQLite, not just apply().
            'core_excluding_llm_ms': round(max(0, core_total - provider.elapsed_ms), 2),
            'pipeline_total_ms': round((time.perf_counter() - started) * 1000, 2),
        },
    }
