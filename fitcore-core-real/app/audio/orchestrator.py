"""Durable, ordered complete-WAV processing. No streaming, worker daemon or event API."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import Field

from app import service, events, finish
from app.asr.base import ASRResult
from app.asr.numeric_recheck import ASR_NUMERIC_RECHECK_REASONS
from app.audio.recording import validate_wav
from app.db import connect, init_db
from app.schemas import SpeakerDecision, StrictModel, UtteranceIn

MAX_AUDIO_WAV_BYTES = 7_000_000


class AudioConflict(ValueError):
    pass


class SessionBusy(RuntimeError):
    pass


class AudioSubmission(StrictModel):
    utterance_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(ge=0, strict=True)
    audio_source: str = Field(min_length=1, max_length=1000)
    source_type: Literal['trusted_test_text', 'authenticated_audio'] = 'trusted_test_text'
    # Source trust is supplied by the caller, never inferred from ASR success.
    speaker_status: SpeakerDecision = SpeakerDecision.UNCERTAIN


class SessionLock:
    """OS-held file lock: shared across processes, automatically released on process death.

    No expiring lease that could let a stale LLM worker commit after a new worker.
    Locks must live beside the same local SQLite file for all cooperating workers.
    """
    def __init__(self, database_url, sid):
        path = Path(database_url.removeprefix('sqlite:///')).resolve()
        folder = path.parent / (path.name + '.audio-locks')
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / (hashlib.sha256(sid.encode()).hexdigest() + '.lock')

    def __enter__(self):
        self.file = self.path.open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise SessionBusy('SESSION_PROCESSING_IN_PROGRESS') from None
        return self

    def __exit__(self, *args):
        try:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()


def head_job(db, sid):
    row = db.execute("SELECT * FROM audio_jobs WHERE session_id=? AND status!='done' ORDER BY sequence LIMIT 1",
                     (sid,)).fetchone()
    if row is None:
        return None
    count = db.execute("SELECT count(*) FROM audio_jobs WHERE session_id=? AND sequence<? AND status='done'",
                       (sid, row['sequence'])).fetchone()[0]
    # Missing earlier sequence or unfinished job is a barrier, not permission to skip.
    return row if count == row['sequence'] else None


def update_job(db, sid, uid, **values):
    allowed = {'status', 'final_text', 'asr_attempts', 'llm_attempts', 'error_type',
               'outcome_json', 'asr_diagnostics_json'}
    if not values or not values.keys() <= allowed:
        raise ValueError('INVALID_JOB_UPDATE')
    owns = not db.in_transaction
    if owns:
        db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('UPDATE audio_jobs SET ' + ','.join(k + '=?' for k in values) +
                   ',updated_at=CURRENT_TIMESTAMP WHERE session_id=? AND utterance_id=?',
                   (*values.values(), sid, uid))
        if values.get('final_text') is not None:
            row = db.execute('SELECT audio_source,audio_sha256,sequence FROM audio_jobs WHERE session_id=? AND utterance_id=?', (sid, uid)).fetchone()
            events.append(db, sid, f'final:audio:{uid}', 'TRANSCRIPT_FINAL', uid,
                          {'text': values['final_text'], **dict(row)})
        if values.get('status') in {'retry_asr', 'retry_llm', 'blocked'}:
            stage = 'asr' if values['status'] == 'retry_asr' else 'semantic'
            key = f'error:audio:{uid}:asr' if stage == 'asr' else f'error:utterance:{uid}:semantic'
            events.append(db, sid, key, 'PROCESSING_ERROR', uid,
                          {'stage': stage, 'error_type': values.get('error_type')})
        if owns:
            db.commit()
    except Exception:
        if owns:
            db.rollback()
        raise


def public_job(db, sid, uid):
    row = db.execute('SELECT session_id,utterance_id,sequence,audio_sha256,audio_source,source_type,'
                     'speaker_status,audio_metadata_json,status,final_text,asr_attempts,llm_attempts,'
                     'error_type,outcome_json,asr_diagnostics_json FROM audio_jobs '
                     'WHERE session_id=? AND utterance_id=?', (sid, uid)).fetchone()
    if row is None:
        return None
    result = dict(row)
    for name in ('audio_metadata', 'outcome', 'asr_diagnostics'):
        value = result.pop(name + '_json')
        result[name] = json.loads(value) if value else None
    return result


class FinalUtteranceProcessor:
    """Frozen FINAL -> existing validated semantics and transactional Fact Engine."""
    def __init__(self, database_url, semantic_provider, asr_provider=None, recheck_enabled=False):
        self.database_url = database_url
        self.semantic_provider = semantic_provider
        self.asr_provider = asr_provider
        self.recheck_enabled = recheck_enabled

    def process_next(self, sid):
        with SessionLock(self.database_url, sid):
            db = connect(self.database_url)
            try:
                row = head_job(db, sid)
                if row is None or not row['final_text'] or row['status'] == 'blocked':
                    return None
                self._process_locked(db, row)
                return public_job(db, sid, row['utterance_id'])
            finally:
                db.close()

    def _process_locked(self, db, row):
        sid, uid = row['session_id'], row['utterance_id']
        request = UtteranceIn(utterance_id=uid, transcript=row['final_text'],
                              source_type=row['source_type'], speaker_status=row['speaker_status'])
        # Core may have committed before a crash but audio_jobs was not acknowledged.
        # Its persistent idempotency check handles that gap without calling LLM again.
        update_job(db, sid, uid, status='llm_running', llm_attempts=row['llm_attempts'] + 1, error_type=None)
        try:
            result = service.process(db, self.semantic_provider, sid, request)
        except Exception:
            update_job(db, sid, uid, status='retry_llm', error_type='CORE_EXCEPTION')
            return
        if result is None:
            update_job(db, sid, uid, status='blocked', error_type='SESSION_NOT_FOUND')
            return
        if result.decision in {'ERROR', 'STALE_CONTEXT'}:
            diagnostic = getattr(self.semantic_provider, 'last_diagnostics', {})
            # Only fixed diagnostic identifiers, no exception/remote body/credentials.
            code = diagnostic.get('error_type')
            safe_code = code if code in {'NETWORK_TIMEOUT', 'HTTP_STATUS_ERROR', 'NETWORK_ERROR',
                'MODEL_OUTPUT_SCHEMA_ERROR', 'MODEL_OUTPUT_NOT_JSON', 'MODEL_OUTPUT_TRUNCATED'} else result.reason
            update_job(db, sid, uid, status='retry_llm', error_type=safe_code,
                       outcome_json=result.model_dump_json())
        elif result.decision == 'ID_CONFLICT':
            update_job(db, sid, uid, status='blocked', error_type='ID_CONFLICT', outcome_json=result.model_dump_json())
        else:
            # Optional second-pass ASR on transcript-level numeric ambiguity.
            # Evidence only: the pending stays open and the decision stands.
            if (self.recheck_enabled and self.asr_provider is not None
                    and result.decision == 'DEFER'
                    and result.reason in ASR_NUMERIC_RECHECK_REASONS
                    and row['audio_wav']):
                from app.asr.numeric_recheck import run_numeric_recheck
                evidence = run_numeric_recheck(self.asr_provider, row['audio_wav'], row['final_text'])
                service.attach_asr_evidence(db, sid, uid, evidence)
            # COMMIT / DUPLICATE / DEFER / REJECT are completed Core decisions.
            update_job(db, sid, uid, status='done', error_type=None, outcome_json=result.model_dump_json())


class AudioOrchestrator:
    def __init__(self, database_url, asr_provider, semantic_provider, recheck_enabled=False):
        if not database_url.startswith('sqlite:///') or database_url == 'sqlite:///:memory:':
            raise ValueError('Durable file SQLite is required')
        self.database_url = database_url
        self.asr_provider = asr_provider
        self.final_processor = FinalUtteranceProcessor(database_url, semantic_provider,
                                                       asr_provider=asr_provider,
                                                       recheck_enabled=recheck_enabled)
        db = connect(database_url)
        try:
            init_db(db)
        finally:
            db.close()

    def enqueue(self, sid, submission: AudioSubmission, wav_path: Path):
        if not isinstance(submission, AudioSubmission):
            raise TypeError('AudioSubmission required')
        with Path(wav_path).open('rb') as audio:
            raw = audio.read(MAX_AUDIO_WAV_BYTES + 1)
        if len(raw) > MAX_AUDIO_WAV_BYTES:
            raise ValueError('AUDIO_TOO_LARGE')
        # Validate exactly the immutable bytes that will be persisted, not a mutable source file.
        with tempfile.TemporaryDirectory(prefix='fitcore-validate-') as folder:
            path = Path(folder) / 'complete.wav'
            path.write_bytes(raw)
            metadata = validate_wav(path)
        digest = hashlib.sha256(raw).hexdigest()
        db = connect(self.database_url)
        try:
            db.execute('BEGIN IMMEDIATE')
            if service.context(db, sid) is None:
                raise AudioConflict('SESSION_NOT_FOUND')
            old = db.execute('SELECT * FROM audio_jobs WHERE session_id=? AND utterance_id=?',
                             (sid, submission.utterance_id)).fetchone()
            if old:
                same = (old['audio_sha256'] == digest and all(old[k] == v for k, v in submission.model_dump().items()))
                if not same:
                    raise AudioConflict('UTTERANCE_ID_CONFLICT')
            else:
                try:
                    finish.check_enqueue(db, sid, submission.sequence)
                except finish.FinishConflict as exc:
                    raise AudioConflict(str(exc)) from exc
                if service.context(db, sid).status != 'active':
                    raise AudioConflict('SESSION_CLOSED')
                if db.execute('SELECT 1 FROM audio_jobs WHERE session_id=? AND sequence=?',
                              (sid, submission.sequence)).fetchone():
                    raise AudioConflict('SEQUENCE_CONFLICT')
                # Content hashes check identity conflicts, not semantic equality: users can say
                # identical words in distinct sets. IDs + explicit sequence define a segment.
                db.execute('INSERT INTO audio_jobs(session_id,utterance_id,sequence,audio_sha256,audio_wav,'
                           'audio_source,source_type,speaker_status,audio_metadata_json) VALUES (?,?,?,?,?,?,?,?,?)',
                           (sid, submission.utterance_id, submission.sequence, digest, raw, submission.audio_source,
                            submission.source_type, submission.speaker_status.value, json.dumps(metadata)))
            db.commit()
            return public_job(db, sid, submission.utterance_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, sid, uid):
        db = connect(self.database_url)
        try:
            return public_job(db, sid, uid)
        finally:
            db.close()

    def process_next(self, sid):
        with SessionLock(self.database_url, sid):
            db = connect(self.database_url)
            try:
                row = head_job(db, sid)
                if row is None:
                    return None
                uid = row['utterance_id']
                if row['status'] == 'blocked':
                    return public_job(db, sid, uid)
                if row['final_text'] is None:
                    update_job(db, sid, uid, status='asr_running', asr_attempts=row['asr_attempts'] + 1, error_type=None)
                    try:
                        with tempfile.TemporaryDirectory(prefix='fitcore-asr-') as folder:
                            path = Path(folder) / 'complete.wav'
                            path.write_bytes(row['audio_wav'])
                            result = self.asr_provider.transcribe_file(path)
                        # ASRProvider contract returns only validated final results. Official adapter
                        # rejects sentence_end=false. No streaming/partial result entry point exists.
                        if (not isinstance(result, ASRResult) or result.http_status != 200 or
                            not result.transcript.strip() or len(result.transcript.strip()) > 2000):
                            raise ValueError('INVALID_ASR_FINAL')
                        update_job(db, sid, uid, final_text=result.transcript.strip(), status='final_ready',
                                   error_type=None, asr_diagnostics_json=result.model_dump_json())
                    except Exception:
                        diagnostic = getattr(self.asr_provider, 'diagnostics', {})
                        code = diagnostic.get('error_type')
                        safe_code = code if code in {'ASR_TIMEOUT', 'ASR_HTTP_ERROR', 'ASR_NETWORK_OR_JSON_ERROR',
                            'ASR_RESPONSE_SCHEMA_ERROR'} else 'ASR_FAILED'
                        update_job(db, sid, uid, status='retry_asr', error_type=safe_code)
                        return public_job(db, sid, uid)
                # A committed FINAL checkpoint exists before invoking any semantic provider.
                row = db.execute('SELECT * FROM audio_jobs WHERE session_id=? AND utterance_id=?', (sid, uid)).fetchone()
                self.final_processor._process_locked(db, row)
                return public_job(db, sid, uid)
            finally:
                db.close()

    def drain(self, sid, max_jobs=10):
        if not 1 <= max_jobs <= 100:
            raise ValueError('max_jobs must be 1..100')
        results = []
        for _ in range(max_jobs):
            result = self.process_next(sid)
            if result is None:
                break
            results.append(result)
            if result['status'] != 'done':
                break  # Explicit retry; never loop forever on the failed head job.
        return results
