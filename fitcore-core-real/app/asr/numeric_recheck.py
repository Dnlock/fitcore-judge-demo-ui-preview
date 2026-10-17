"""Pluggable numeric re-check for ASR ambiguity (A01 second-pass recognition).

The first ASR pass may fuse spoken numbers ("60，7个" -> "67个"). When the Core
defers with an ASR numeric-ambiguity reason, the orchestration layer MAY call an
optional second ASR pass and persist the transcript evidence onto the pending
payload. This module never changes the decision or writes facts on its own:
  - every re-check outcome (candidates / divergence / provider error) is stored
    as pending.payload_json['asr_evidence'] only
  - the pending stays open; a human/supplement confirmation still applies the
    set through the existing resolve API
  - feature defaults OFF (Settings.numeric_recheck_enabled=False)
Real-device re-checks remain BLOCKED until real audio + provider authorization
are granted; the deterministic parts below are fully offline-testable.
"""
import re
import tempfile
from pathlib import Path

from app.asr.base import ASRResult
from app.semantic.grounding import NUM, decimal_numeric

# Only transcript-level fused-number ambiguity triggers a re-check. Model
# grounding problems (NUMERIC_EVIDENCE_WEIGHT_NOT_IN_TEXT, unreliable context
# references) are semantic-side issues a second ASR pass cannot heal.
ASR_NUMERIC_RECHECK_REASONS = frozenset({'ASR_NUMERIC_AMBIGUITY_REQUIRES_CONFIRMATION'})

_NUMBER = re.compile(NUM)


def numeric_sequence(text):
    """Ordered numeric tokens (Arabic/Chinese) as canonical strings.
    Unsupported tokens are dropped; nothing is invented."""
    values = []
    for match in _NUMBER.finditer(text):
        token = match.group(0)
        # Ordinal contexts (set numbers / "第N") are not training quantities.
        if text[match.end():match.end() + 1] == '组':
            continue
        if match.start() > 0 and text[match.start() - 1] == '第':
            continue
        value = decimal_numeric(token)
        if value is not None:
            values.append(format(value, 'f'))
    return values


def compare_numeric_transcripts(original, candidates):
    """Evidence dict comparing transcript numeric sequences.

    converged=True only when every candidate carries the SAME numeric sequence
    as the original transcript. Anything else (including split-vs-fused
    divergence like '67个' vs '60，7个') is recorded as divergent evidence and
    the pending stays open for confirmation.
    """
    original_seq = numeric_sequence(original)
    candidate_entries = [{'transcript': c.strip(), 'numeric_sequence': numeric_sequence(c)}
                         for c in candidates]
    converged = bool(candidate_entries) and all(
        e['numeric_sequence'] == original_seq for e in candidate_entries)
    return {
        'original_transcript': original.strip(),
        'original_numeric_sequence': original_seq,
        'candidates': candidate_entries,
        'converged': converged,
        'kind': 'consistent' if converged else 'divergent',
        'source': 'asr_second_pass',
    }


def run_numeric_recheck(asr_provider, audio_bytes, original_text):
    """One bounded second ASR pass. Never raises: failures become evidence."""
    with tempfile.TemporaryDirectory(prefix='fitcore-recheck-') as folder:
        try:
            path = Path(folder) / 'complete.wav'
            path.write_bytes(audio_bytes)
            try:
                result = asr_provider.transcribe_file(path)
            except Exception:
                diagnostic = getattr(asr_provider, 'diagnostics', {})
                code = diagnostic.get('error_type')
                safe = code if code in {'ASR_TIMEOUT', 'ASR_HTTP_ERROR',
                                        'ASR_NETWORK_OR_JSON_ERROR',
                                        'ASR_RESPONSE_SCHEMA_ERROR'} else 'ASR_RECHECK_FAILED'
                return {'error': safe, 'source': 'asr_second_pass'}
            if (not isinstance(result, ASRResult) or result.http_status != 200 or
                    not result.transcript.strip()):
                return {'error': 'INVALID_ASR_RECHECK_RESULT', 'source': 'asr_second_pass'}
            return compare_numeric_transcripts(original_text, [result.transcript])
        except Exception:  # safety net: evidence gathering never breaks the pipeline
            return {'error': 'ASR_RECHECK_FAILED', 'source': 'asr_second_pass'}