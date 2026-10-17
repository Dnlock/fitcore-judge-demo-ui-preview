"""Contest ASR adapter. No fake transcript or automatic public file hosting."""
import base64
import os
import re
import time
from urllib.parse import urlparse
from pathlib import Path

import httpx

from app.audio.recording import validate_wav
from app.asr.base import ASRResult
from app.config import Settings


class ASRUnavailable(Exception):
    pass


class RealASRProvider:
    def __init__(self, settings: Settings, transport=None, route_provider: str | None = None):
        self.settings = settings
        self.transport = transport
        self.route_provider = route_provider if route_provider is not None else os.getenv('ASR_PROVIDER_HEADER', 'dashscope_ audio')
        self.diagnostics = {}

    def transcribe_file(self, path: Path) -> ASRResult:
        self.diagnostics = {'request_sent': False, 'http_status': None, 'elapsed_ms': None,
                            'schema_parse_success': False, 'error_type': None,
                            'transport': 'mock' if self.transport else 'real_network'}
        if not self.settings.model_api_key:
            self.diagnostics['error_type'] = 'CONFIG_MISSING'
            raise ASRUnavailable('MODEL_API_KEY is not configured')
        if '\r' in self.route_provider or '\n' in self.route_provider or self.route_provider != self.route_provider.strip():
            self.diagnostics['error_type'] = 'ASR_PROVIDER_HEADER_INVALID'
            raise ASRUnavailable('ASR_PROVIDER_HEADER must not contain line breaks or surrounding whitespace')
        metadata = validate_wav(path)
        raw = path.read_bytes()
        if len(raw) * 4 / 3 > 10_000_000:
            raise ValueError('Audio Data URI exceeds the documented 10MB limit')
        base = os.getenv('ASR_BASE_URL', self.settings.model_base_url).rstrip('/')
        address = urlparse(base)
        if address.scheme != 'https' or not address.netloc or address.query or address.fragment:
            raise ASRUnavailable('ASR_BASE_URL must be HTTPS')
        headers = {'Authorization': 'Bearer ' + self.settings.model_api_key,
                   'Content-Type': 'application/json', 'X-DashScope-SSE': 'disable'}
        # The internal space is part of the live-verified contest routing key.
        # Do not normalize it to dashscope_audio, which returns provider config not found.
        if self.route_provider:
            headers['x-model-provider'] = self.route_provider
        payload = {
            'model': os.getenv('ASR_MODEL_ID', 'hackathon/v_model/qwen-audio-3.0-asr-flash'),
            'input': {'messages': [{'role': 'user', 'content': [{'type': 'input_audio',
                      'input_audio': {'data': 'data:audio/wav;base64,' + base64.b64encode(raw).decode('ascii')}}]}]},
            'parameters': {'format': 'wav', 'sample_rate': '16000'},
        }
        started = time.perf_counter()
        body_started = None
        def trace(name, info):
            nonlocal body_started
            # Record durations only; trace info can contain private request data.
            if name.endswith('send_request_body.started'):
                body_started = time.perf_counter()
            elif name.endswith('send_request_body.complete') and body_started is not None:
                self.diagnostics['upstream_body_send_ms'] = round((time.perf_counter() - body_started) * 1000, 2)
        try:
            with httpx.Client(timeout=float(os.getenv('ASR_TIMEOUT_SECONDS', '60')),
                              transport=self.transport, follow_redirects=False) as client:
                self.diagnostics['request_sent'] = None
                response = client.post(base + '/openai_passthrough/services/aigc/multimodal-generation/generation',
                                       headers=headers, json=payload, extensions={'trace': trace})
                self.diagnostics['request_sent'] = True
                self.diagnostics['http_status'] = response.status_code
                response.raise_for_status()
                result = response.json()
        except httpx.TimeoutException:
            self.diagnostics['error_type'] = 'ASR_TIMEOUT'
            raise ASRUnavailable('ASR_TIMEOUT') from None
        except httpx.HTTPStatusError as exc:
            self.diagnostics['error_type'] = 'ASR_HTTP_ERROR'
            try:
                error_body = exc.response.json()
                error = error_body.get('error', error_body) if isinstance(error_body, dict) else {}
                if not isinstance(error, dict):
                    error = {}
                for source, target in [('code', 'upstream_code'), ('message', 'upstream_message')]:
                    value = error.get(source)
                    if isinstance(value, (str, int)):
                        text = str(value).replace(self.settings.model_api_key, '[REDACTED]')
                        text = re.sub(r'data:[^\s\"\']+;base64,[A-Za-z0-9+/=]+', '[AUDIO_REDACTED]', text)
                        text = re.sub(r'(?i)Bearer\s+[^\s\"\']+', 'Bearer [REDACTED]', text)
                        self.diagnostics[target] = text[:500]
            except ValueError:
                self.diagnostics['upstream_message'] = 'Non-JSON error body omitted'
            raise ASRUnavailable('ASR_HTTP_ERROR') from None
        except (httpx.HTTPError, ValueError):
            self.diagnostics['error_type'] = 'ASR_NETWORK_OR_JSON_ERROR'
            raise ASRUnavailable('ASR_NETWORK_OR_JSON_ERROR') from None
        finally:
            self.diagnostics['elapsed_ms'] = round((time.perf_counter() - started) * 1000)
        try:
            output = result.get('output', {})
            transcript = output.get('text', result.get('text'))
            sentence = output.get('sentence', {})
            if not isinstance(transcript, str) or not transcript.strip() or sentence.get('sentence_end') is False:
                raise ValueError()
            if result.get('text') is not None and output.get('text') is not None and result['text'] != output['text']:
                raise ValueError()
            duration = result.get('usage', {}).get('duration')
            if duration is not None and (not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            self.diagnostics['error_type'] = 'ASR_RESPONSE_SCHEMA_ERROR'
            raise ASRUnavailable('ASR_RESPONSE_SCHEMA_ERROR') from None
        self.diagnostics['schema_parse_success'] = True
        return ASRResult(transcript=transcript.strip(), audio_duration_seconds=metadata['duration_seconds'],
                         provider_duration_seconds=duration, elapsed_ms=self.diagnostics['elapsed_ms'],
                         http_status=response.status_code)
