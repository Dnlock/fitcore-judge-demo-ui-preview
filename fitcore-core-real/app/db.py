import sqlite3
from pathlib import Path


def connect(database_url: str):
    if not database_url.startswith("sqlite:///"):
        raise ValueError("Only sqlite:/// URLs are supported")
    path = database_url[len("sqlite:///"):]
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    return db


def init_db(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 0,
      active_exercise_instance_id INTEGER, last_completed_set_id INTEGER,
      last_reported_weight REAL, next_set_plan REAL, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
      ended_at TEXT);
    CREATE TABLE IF NOT EXISTS exercise_instances (
      id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
      exercise_name TEXT NOT NULL, sequence INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS utterances (
      session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL, transcript TEXT NOT NULL,
      source_type TEXT NOT NULL, speaker_status TEXT NOT NULL, request_hash TEXT NOT NULL,
      context_version_at_parse INTEGER NOT NULL, processing_status TEXT NOT NULL, reason TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(session_id,id));
    CREATE TABLE IF NOT EXISTS semantic_operations (
      session_id TEXT NOT NULL, utterance_id TEXT NOT NULL, operation_index INTEGER NOT NULL,
      type TEXT NOT NULL, payload_json TEXT NOT NULL,
      PRIMARY KEY(session_id,utterance_id,operation_index),
      FOREIGN KEY(session_id,utterance_id) REFERENCES utterances(session_id,id));
    CREATE TABLE IF NOT EXISTS sets (
      id INTEGER PRIMARY KEY, exercise_instance_id INTEGER NOT NULL REFERENCES exercise_instances(id),
      set_number INTEGER NOT NULL, weight_kg REAL NOT NULL, reps INTEGER NOT NULL,
      weight_provenance TEXT NOT NULL, weight_source_fact_id INTEGER,
      source_utterance_id TEXT NOT NULL, is_voided INTEGER NOT NULL DEFAULT 0,
      UNIQUE(exercise_instance_id,set_number));
    CREATE TABLE IF NOT EXISTS training_notes (
      id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
      set_id INTEGER, text TEXT NOT NULL, source_utterance_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS next_set_plans (
      id INTEGER PRIMARY KEY, exercise_instance_id INTEGER NOT NULL REFERENCES exercise_instances(id),
      weight_kg REAL NOT NULL, base_set_id INTEGER, source_utterance_id TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active');
    CREATE TABLE IF NOT EXISTS pending_operations (
      id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
      utterance_id TEXT NOT NULL, reason TEXT NOT NULL, payload_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open', context_version_at_defer INTEGER,
      exercise_instance_id INTEGER, last_completed_set_id INTEGER);
    CREATE TABLE IF NOT EXISTS pending_resolutions (
      session_id TEXT NOT NULL REFERENCES sessions(id), pending_id INTEGER NOT NULL REFERENCES pending_operations(id),
      resolution_id TEXT NOT NULL, request_hash TEXT NOT NULL, action TEXT NOT NULL,
      resulting_version INTEGER NOT NULL, PRIMARY KEY(session_id,resolution_id),
      UNIQUE(pending_id));
    CREATE TABLE IF NOT EXISTS change_log (
      id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
      target_type TEXT NOT NULL, target_id INTEGER NOT NULL, before_json TEXT NOT NULL,
      after_json TEXT NOT NULL, source_utterance_id TEXT NOT NULL,
      changed_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS semantic_reparses (
      session_id TEXT NOT NULL, utterance_id TEXT NOT NULL,
      pending_id INTEGER NOT NULL REFERENCES pending_operations(id),
      state TEXT NOT NULL CHECK(state IN ('in_flight','ready','failed','applied','blocked')),
      context_json TEXT NOT NULL, original_json TEXT NOT NULL, diagnostics_json TEXT NOT NULL,
      result_json TEXT, outcome_json TEXT, provider_diagnostics_json TEXT, elapsed_ms REAL,
      reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(session_id,utterance_id),
      FOREIGN KEY(session_id,utterance_id) REFERENCES utterances(session_id,id));
    CREATE TABLE IF NOT EXISTS session_finishes (
      session_id TEXT PRIMARY KEY REFERENCES sessions(id), finish_id TEXT NOT NULL,
      last_sequence INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('awaiting_audio','draining','finished')),
      manifest_json TEXT, review_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      closed_at TEXT, finished_at TEXT);
    CREATE TABLE IF NOT EXISTS session_events (
      session_id TEXT NOT NULL REFERENCES sessions(id), event_seq INTEGER NOT NULL,
      event_key TEXT NOT NULL, type TEXT NOT NULL CHECK(type IN
        ('TRANSCRIPT_FINAL','FACT_COMMITTED','OPERATION_DEFERRED','INPUT_REJECTED','PROCESSING_ERROR')),
      utterance_id TEXT, payload_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(session_id,event_seq), UNIQUE(session_id,event_key));
    CREATE TABLE IF NOT EXISTS audio_jobs (
      session_id TEXT NOT NULL REFERENCES sessions(id), utterance_id TEXT NOT NULL,
      sequence INTEGER NOT NULL CHECK(sequence>=0), audio_sha256 TEXT NOT NULL,
      audio_wav BLOB NOT NULL, audio_source TEXT NOT NULL, source_type TEXT NOT NULL,
      speaker_status TEXT NOT NULL, audio_metadata_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN
        ('queued','asr_running','final_ready','llm_running','retry_asr','retry_llm','blocked','done')),
      final_text TEXT, asr_attempts INTEGER NOT NULL DEFAULT 0, llm_attempts INTEGER NOT NULL DEFAULT 0,
      error_type TEXT, outcome_json TEXT, asr_diagnostics_json TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(session_id,utterance_id), UNIQUE(session_id,sequence));
    """)
    # The first phase already created local SQLite files with the shorter pending table.
    columns = {row[1] for row in db.execute("PRAGMA table_info(pending_operations)")}
    for name, definition in (
        ("context_version_at_defer", "INTEGER"),
        ("exercise_instance_id", "INTEGER"),
        ("last_completed_set_id", "INTEGER"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE pending_operations ADD COLUMN {name} {definition}")
