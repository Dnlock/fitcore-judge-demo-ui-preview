"""Single-instance local SQLite queue consumer. Reuses the existing orchestrator."""
import threading
import time
from app import finish
from app.db import connect
from app.audio.orchestrator import SessionLock, SessionBusy, head_job


class AudioWorker:
    def __init__(self, settings, orchestrator):
        self.settings, self.orchestrator = settings, orchestrator
        self.stopping = threading.Event()
        self.thread = None
        self.last_error = None
        self.retry_after = {}
        self.lock = SessionLock(settings.database_url + '.http-worker', 'singleton')

    def start(self):
        try:
            self.lock.__enter__()
        except SessionBusy:
            raise RuntimeError('AUDIO_WORKER_ALREADY_RUNNING_USE_ONE_SERVER_PROCESS') from None
        self.thread = threading.Thread(target=self.run, name='fitcore-audio-worker', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join()  # Finish an in-flight bounded provider call before relinquishing ownership.
            self.lock.__exit__(None, None, None)
            self.thread = None

    def run(self):
        while not self.stopping.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception:
                # Never log arbitrary upstream exceptions or tokens.
                self.last_error = 'AUDIO_WORKER_TICK_FAILED'
            self.stopping.wait(self.settings.audio_worker_poll_seconds)

    def tick(self):
        db = connect(self.settings.database_url)
        try:
            sids = [r[0] for r in db.execute("SELECT id FROM sessions WHERE status='active' ORDER BY started_at,id")]
            for sid in sids:
                if self.stopping.is_set():
                    break
                row = head_job(db, sid)
                if row and row['status'] != 'blocked':
                    key = (sid, row['utterance_id'])
                    if time.monotonic() < self.retry_after.get(key, 0):
                        continue
                    attempts = row['asr_attempts'] if row['final_text'] is None else row['llm_attempts']
                    acknowledged = db.execute('SELECT 1 FROM utterances WHERE session_id=? AND id=?',key).fetchone()
                    if attempts >= self.settings.audio_worker_max_attempts and not acknowledged:
                        continue
                    try:
                        result = self.orchestrator.process_next(sid)
                    except SessionBusy:
                        continue
                    if result and result['status'] in {'retry_asr','retry_llm'}:
                        self.retry_after[key] = time.monotonic() + self.settings.audio_worker_retry_seconds
                    else:
                        self.retry_after.pop(key, None)
                barrier = db.execute("SELECT * FROM session_finishes WHERE session_id=? AND state='draining'", (sid,)).fetchone()
                if barrier and not db.execute("SELECT 1 FROM audio_jobs WHERE session_id=? AND status!='done' LIMIT 1", (sid,)).fetchone():
                    finish.request(db, sid, finish.FinishIn(finish_id=barrier['finish_id'],last_sequence=barrier['last_sequence']))
        finally:
            db.close()
