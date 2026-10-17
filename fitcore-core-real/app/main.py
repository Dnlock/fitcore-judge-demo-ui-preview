import sqlite3
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Response
from app import events
from app import finish
from app.config import Settings
from app.db import connect, init_db
from app.schemas import (ContextRead, FactRead, PendingRead, PendingResolutionIn, PendingResolutionOut,
                         ReviewRead, SessionCreate, UtteranceIn, UtteranceOut)
from app.semantic.fake_provider import FakeSemanticProvider
from app.semantic.real_provider import RealModelProvider
from app import service


def create_app(settings: Settings | None = None, provider=None, asr_provider=None):
    settings = settings or Settings.from_env()
    if (settings.audio_worker_poll_seconds<=0 or settings.audio_worker_retry_seconds<0 or
            settings.audio_worker_max_attempts not in range(1,6)):
        raise ValueError('INVALID_AUDIO_WORKER_CONFIGURATION')
    if settings.audio_worker_enabled and not settings.http_api_token:
        raise ValueError('AUDIO_HTTP_WORKER_REQUIRES_API_TOKEN')
    @asynccontextmanager
    async def lifespan(app):
        if settings.audio_worker_enabled:
            app.state.audio_worker.start()
        try:
            yield
        finally:
            if settings.audio_worker_enabled:
                await asyncio.to_thread(app.state.audio_worker.stop)
    app = FastAPI(title="Fitcore Core", version="0.1.0",lifespan=lifespan)
    app.state.settings = settings
    app.state.provider = provider or (FakeSemanticProvider() if settings.model_provider == "fake" else RealModelProvider(settings))
    db = connect(settings.database_url)
    init_db(db)
    db.close()

    def database():
        return connect(settings.database_url)

    from app.asr.real_provider import RealASRProvider
    from app.audio.orchestrator import AudioOrchestrator
    from app.audio.worker import AudioWorker
    from app.audio.http import HTTPBoundary,router
    app.state.audio_database = database
    app.state.audio_orchestrator = AudioOrchestrator(settings.database_url,
        asr_provider or RealASRProvider(settings), app.state.provider,
        recheck_enabled=settings.numeric_recheck_enabled)
    app.state.audio_worker = AudioWorker(settings,app.state.audio_orchestrator)
    app.add_middleware(HTTPBoundary,settings=settings)
    app.include_router(router(app))

    @app.get("/health")
    def health():
        return {"status": "ok", "model_provider": settings.model_provider}

    @app.post("/v1/sessions", response_model=ContextRead, status_code=201)
    def start_session(body: SessionCreate):
        db = database()
        try:
            return service.start(db, body)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "SESSION_ID_EXISTS")
        finally:
            db.close()

    def require(db, sid):
        value = service.context(db, sid)
        if value is None:
            raise HTTPException(404, "SESSION_NOT_FOUND")
        return value

    @app.get("/v1/sessions/{sid}", response_model=ContextRead)
    def get_session(sid: str):
        db = database()
        try:
            return require(db, sid)
        finally:
            db.close()

    @app.post("/v1/sessions/{sid}/utterances", response_model=UtteranceOut)
    def utterance(sid: str, body: UtteranceIn):
        db = database()
        try:
            require(db, sid)
            return service.process(db, app.state.provider, sid, body)
        except sqlite3.DatabaseError as exc:
            raise HTTPException(503, "DATABASE_ERROR_RETRY_SAME_UTTERANCE_ID") from exc
        finally:
            db.close()

    @app.get("/v1/sessions/{sid}/facts", response_model=list[FactRead])
    def get_facts(sid: str):
        db = database()
        try:
            require(db, sid)
            return service.facts(db, sid)
        finally:
            db.close()

    @app.post('/v1/sessions/{sid}/utterances/{uid}/reparse', response_model=UtteranceOut)
    def reparse_utterance(sid: str, uid: str):
        from app.semantic.reparse import recover
        from app.audio.orchestrator import SessionLock
        with SessionLock(settings.database_url, sid):
            db = database()
            try:
                require(db, sid)
                return recover(db, app.state.provider, sid, uid)
            except sqlite3.DatabaseError as exc:
                raise HTTPException(503, 'REPARSE_DATABASE_ERROR_RETRY_SAME_UTTERANCE_ID') from exc
            finally:
                db.close()

    @app.get('/v1/sessions/{sid}/events')
    def get_events(sid: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100)):
        db = database()
        try:
            require(db, sid)
            return events.query(db, sid, after, limit)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            db.close()

    @app.get("/v1/sessions/{sid}/pending", response_model=list[PendingRead])
    def get_pending(sid: str):
        db = database()
        try:
            require(db, sid)
            return service.pending(db, sid)
        finally:
            db.close()

    @app.post("/v1/sessions/{sid}/pending/{pending_id}/resolve", response_model=PendingResolutionOut)
    def resolve_pending(sid: str, pending_id: int, body: PendingResolutionIn):
        db = database()
        try:
            require(db, sid)
            return service.resolve_pending(db, sid, pending_id, body)
        except service.PendingConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            raise HTTPException(503, "DATABASE_ERROR_RETRY_SAME_RESOLUTION_ID") from exc
        finally:
            db.close()

    @app.post("/v1/sessions/{sid}/end", response_model=UtteranceOut)
    def end_session(sid: str, body: UtteranceIn):
        db = database()
        try:
            require(db, sid)
            if db.execute('SELECT 1 FROM audio_jobs WHERE session_id=? LIMIT 1', (sid,)).fetchone():
                raise HTTPException(409, 'USE_AUDIO_FINISH_BARRIER')
        finally:
            db.close()
        if body.transcript != "今天到这里":
            raise HTTPException(422, "USE_SESSION_END_UTTERANCE")
        return utterance(sid, body)

    @app.get("/v1/sessions/{sid}/review", response_model=ReviewRead)
    def get_review(sid: str):
        db = database()
        try:
            db.execute('BEGIN')
            require(db, sid)
            blocker = finish.review_blocker(db, sid)
            if blocker:
                raise HTTPException(409, blocker)
            return service.review(db, sid)
        finally:
            db.close()

    @app.post('/v1/sessions/{sid}/finish')
    def finish_session(sid: str, body: finish.FinishIn, response: Response):
        db = database()
        try:
            require(db, sid)
            result = finish.request(db, sid, body)
            response.status_code = 200 if result['complete'] else (409 if result['state'] == 'awaiting_audio' else 202)
            return result
        except finish.FinishConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            raise HTTPException(503, 'FINISH_DATABASE_ERROR_RETRY_SAME_ID') from exc
        finally:
            db.close()

    @app.get('/v1/sessions/{sid}/finish')
    def get_finish(sid: str):
        db = database()
        try:
            require(db, sid)
            result = finish.status(db, sid)
            if result is None:
                raise HTTPException(404, 'FINISH_NOT_REQUESTED')
            return result
        finally:
            db.close()

    return app


app = create_app()
