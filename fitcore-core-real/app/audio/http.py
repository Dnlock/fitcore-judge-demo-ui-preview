"""Bounded full-WAV HTTP ingress, distinct from streaming audio-chunks."""
import hmac
import sqlite3
from pathlib import Path
import tempfile
import wave
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from starlette.responses import JSONResponse
from app.audio.orchestrator import AudioConflict, AudioSubmission, MAX_AUDIO_WAV_BYTES

MAX_MULTIPART_BYTES = MAX_AUDIO_WAV_BYTES + 65_536


class HTTPBoundary:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/v1/'):
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        token = self.settings.http_api_token
        local = (scope.get('client') or ('',0))[0] in {'127.0.0.1','::1','testclient'}
        response = None
        if token:
            if not hmac.compare_digest(headers.get(b'authorization',b''), ('Bearer '+token).encode()):
                response = JSONResponse({'detail':'AUTHENTICATION_REQUIRED'},401,headers={'WWW-Authenticate':'Bearer'})
        elif not local:
            response = JSONResponse({'detail':'HTTP_API_TOKEN_REQUIRED_FOR_REMOTE_ACCESS'},403)
        # Text injection is a local development interface, never an authenticated audio ingress.
        if response is None and scope['method']=='POST' and (token or not local) and (
                scope['path'].endswith('/utterances') or scope['path'].endswith('/end')):
            response = JSONResponse({'detail':'TEXT_SOURCE_ASSERTION_DISABLED_USE_AUDIO_JOBS'},403)
        if response is not None:
            return await response(scope, receive, send)
        if scope['method']=='POST' and scope['path'].endswith('/audio-jobs'):
            # Bound the entire body before multipart parsing, independent of Content-Length.
            length = headers.get(b'content-length')
            if length and (not length.isdigit() or int(length)>MAX_MULTIPART_BYTES):
                return await JSONResponse({'detail':'AUDIO_UPLOAD_TOO_LARGE'},413)(scope,receive,send)
            chunks, size = [], 0
            while True:
                message = await receive()
                if message['type']=='http.disconnect':
                    return
                size += len(message.get('body',b''))
                if size>MAX_MULTIPART_BYTES:
                    return await JSONResponse({'detail':'AUDIO_UPLOAD_TOO_LARGE'},413)(scope,receive,send)
                chunks.append(message)
                if not message.get('more_body',False):
                    break
            async def bounded_receive():
                return chunks.pop(0) if chunks else await receive()
            return await self.app(scope,bounded_receive,send)
        return await self.app(scope,receive,send)


def router(app):
    routes = APIRouter()

    @routes.post('/v1/sessions/{sid}/audio-jobs',status_code=202)
    async def upload(sid:str,request:Request,file:UploadFile=File(...),utterance_id:str=Form(...,min_length=1,max_length=120),
                     sequence:int=Form(...,ge=0),audio_source:str=Form('client_complete_wav',min_length=1,max_length=200)):
        form = await request.form()
        if set(form)-{'file','utterance_id','sequence','audio_source'} or any(len(form.getlist(k))!=1 for k in form):
            raise HTTPException(422,'UNSUPPORTED_OR_DUPLICATE_METADATA_SERVER_ASSIGNS_SOURCE_TRUST')
        if not app.state.settings.http_api_token:
            raise HTTPException(503,'CONFIGURE_FITCORE_HTTP_API_TOKEN_FOR_AUDIO_HTTP')
        # Reject a missing Session before spending CPU on validation; enqueue rechecks under its transaction.
        db = app.state.audio_database()
        try:
            if not db.execute('SELECT 1 FROM sessions WHERE id=?',(sid,)).fetchone():
                raise HTTPException(404,'SESSION_NOT_FOUND')
        finally:
            db.close()
        raw = await file.read(MAX_AUDIO_WAV_BYTES+1)
        if len(raw)>MAX_AUDIO_WAV_BYTES:
            raise HTTPException(413,'AUDIO_TOO_LARGE')
        trusted = app.state.settings.audio_test_source_trusted
        submission = AudioSubmission(utterance_id=utterance_id,sequence=sequence,
            audio_source=('http_authorized_test:' if trusted else 'http_unverified:')+audio_source,
            source_type='authenticated_audio',speaker_status='VERIFIED_USER' if trusted else 'UNCERTAIN')
        try:
            from starlette.concurrency import run_in_threadpool
            with tempfile.TemporaryDirectory(prefix='fitcore-upload-') as folder:
                path = Path(folder)/'complete.wav'
                path.write_bytes(raw)
                job = await run_in_threadpool(app.state.audio_orchestrator.enqueue,sid,submission,path)
            return {'acceptance':'persisted','speaker_verification':'not_performed',
                    'source_assurance':'server_authorized_test' if trusted else 'unverified','job':job}
        except AudioConflict as exc:
            raise HTTPException(409,str(exc)) from None
        except sqlite3.DatabaseError:
            raise HTTPException(503,'AUDIO_DATABASE_ERROR_RETRY_SAME_UTTERANCE_ID') from None
        except (ValueError,wave.Error,EOFError):
            raise HTTPException(422,'INVALID_COMPLETE_WAV') from None

    @routes.get('/v1/sessions/{sid}/audio-jobs/{uid}')
    def get_job(sid:str,uid:str):
        job = app.state.audio_orchestrator.get(sid,uid)
        if job is None:
            raise HTTPException(404,'AUDIO_JOB_NOT_FOUND')
        attempts = job['asr_attempts'] if job['final_text'] is None else job['llm_attempts']
        return {'speaker_verification':'not_performed','automatic_retry_exhausted':
                job['status'] not in {'done','blocked'} and attempts>=app.state.settings.audio_worker_max_attempts,'job':job}

    return routes
