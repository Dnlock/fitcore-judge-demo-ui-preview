"""Fixed-request real network diagnostics. Keys stay in process; IPs stay in local logs."""
import base64
import hashlib
import ipaddress
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import httpx

ENDPOINT = 'https://ai-router-cn-pub.anker-in.com/openai_passthrough/services/aigc/multimodal-generation/generation'
MODEL = 'hackathon/v_model/qwen-audio-3.0-asr-flash'


def request_digest(method, url, headers, body):
    canonical = json.dumps({'method': method, 'url': url, 'headers': headers, 'body': body},
                           sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def freeze_request(wav_path: Path, target: Path, provider: str | None = None):
    from app.audio.recording import validate_wav
    metadata = validate_wav(wav_path)
    raw = wav_path.read_bytes()
    headers = {'Content-Type': 'application/json', 'X-DashScope-SSE': 'disable'}
    if provider is not None:
        headers['x-model-provider'] = provider
    payload = {
        'model': MODEL,
        'input': {'messages': [{'role': 'user', 'content': [{'type': 'input_audio',
                  'input_audio': {'data': 'data:audio/wav;base64,' + base64.b64encode(raw).decode('ascii')}}]}]},
        'parameters': {'format': 'wav', 'sample_rate': '16000'},
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    artifact = {'version': 1, 'method': 'POST', 'url': ENDPOINT, 'headers': headers,
                'authorization_source': 'MODEL_API_KEY', 'timeout_seconds': 60,
                'body_utf8': body, 'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                'audio_sha256': hashlib.sha256(raw).hexdigest(), 'audio_metadata': metadata,
                'request_sha256': request_digest('POST', ENDPOINT, headers, body)}
    if target.exists():
        raise ValueError('Frozen request already exists; use it unchanged or choose a new path')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding='utf-8')
    return artifact


def load_request(path: Path):
    artifact = json.loads(path.read_text(encoding='utf-8'))
    if artifact['url'] != ENDPOINT or artifact['method'] != 'POST':
        raise ValueError('Frozen request endpoint/method is not the approved contest ASR endpoint')
    if artifact.get('authorization_source') != 'MODEL_API_KEY':
        raise ValueError('Unexpected credential source')
    if any(key.lower() == 'authorization' for key in artifact['headers']):
        raise ValueError('Frozen request must not contain credentials')
    body = artifact['body_utf8']
    if hashlib.sha256(body.encode()).hexdigest() != artifact['body_sha256']:
        raise ValueError('Frozen body was modified')
    if request_digest(artifact['method'], artifact['url'], artifact['headers'], body) != artifact['request_sha256']:
        raise ValueError('Frozen request parameters were modified')
    if json.loads(body)['model'] != MODEL:
        raise ValueError('Unexpected ASR model')
    return artifact


def redact(text: str, key: str) -> str:
    text = text.replace(key, '[REDACTED]') if key else text
    text = re.sub(r'(?i)Bearer\s+[^\s\"\']+', 'Bearer [REDACTED]', text)
    text = re.sub(r'data:[^\s\"\']+;base64,[A-Za-z0-9+/=]+', '[AUDIO_REDACTED]', text)
    return text[:16384]


def error_message(body: str):
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            error = parsed.get('error', parsed)
            if isinstance(error, dict):
                return str(error.get('message', error.get('detail', '')))
            if isinstance(error, str):
                return error
    except ValueError:
        pass
    return body


def observe_egress():
    # This is the IP seen by an echo service, not guaranteed to be the IP seen by the gateway.
    # PAC/system proxies may choose different routes by destination.
    for url, is_json in [('https://api.ipify.org?format=json', True), ('https://ifconfig.me/ip', False)]:
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                response = client.get(url)
                response.raise_for_status()
                value = response.json()['ip'] if is_json else response.text.strip()
                ip = str(ipaddress.ip_address(value))
                return {'ip': ip, 'service': url, 'http_status': response.status_code,
                        'elapsed_ms': round((time.perf_counter() - started) * 1000),
                        'gateway_observed_ip_confirmed': False}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            continue
    return {'ip': None, 'error_type': 'EGRESS_IP_LOOKUP_FAILED', 'gateway_observed_ip_confirmed': False}


def run_request(artifact: dict, api_key: str, label: str, log_path: Path):
    if not api_key:
        raise ValueError('MODEL_API_KEY is not available; no ASR request sent')
    log = {'label': label, 'timestamp_utc': datetime.now(timezone.utc).isoformat(),
           'request_sha256': artifact['request_sha256'], 'body_sha256': artifact['body_sha256'],
           'model_id': MODEL, 'provider_header': artifact['headers'].get('x-model-provider'),
           'same_credential_required': True, 'credential_equality_verified': False,
           'httpx_version': httpx.__version__, 'system_proxy_configured': bool(urllib.request.getproxies()),
           'egress': observe_egress(), 'http_status': None, 'error_type': None,
           'error_body': None, 'tcp_connect_ms': None, 'tls_handshake_ms': None,
           'response_headers_ms': None, 'request_elapsed_ms': None, 'proxy_tunnel_observed': False}
    phase_start = {}
    sums = {'connect_tcp': 0.0, 'start_tls': 0.0}
    seen = set()
    events = []
    started = time.perf_counter()

    def trace(name, info):
        # Deliberately ignore trace info, which can contain addresses or request internals.
        now = time.perf_counter()
        events.append(name)
        if name.startswith('proxy.'):
            log['proxy_tunnel_observed'] = True
        phase = name.rsplit('.', 1)[0]
        if name.endswith('.started'):
            phase_start[phase] = now
        elif name.endswith(('.complete', '.failed')) and phase in phase_start:
            for suffix in sums:
                if phase.endswith('.' + suffix):
                    sums[suffix] += (now - phase_start[phase]) * 1000
                    seen.add(suffix)
            if phase.endswith('.receive_response_headers') and name.endswith('.complete'):
                log['response_headers_ms'] = round((now - started) * 1000)

    headers = {**artifact['headers'], 'Authorization': 'Bearer ' + api_key}
    try:
        with httpx.Client(timeout=artifact['timeout_seconds'], follow_redirects=False) as client:
            response = client.post(artifact['url'], headers=headers,
                                   content=artifact['body_utf8'].encode('utf-8'), extensions={'trace': trace})
            log['http_status'] = response.status_code
            # Preserve the error response locally, with credentials/audio removed.
            if response.status_code >= 400:
                log['error_body'] = redact(response.text, api_key)
                log['error_type'] = 'HTTP_ERROR'
            else:
                try:
                    payload = response.json()
                    output = payload.get('output', {})
                    text = output.get('text', payload.get('text'))
                    log['transcript_returned'] = isinstance(text, str) and bool(text.strip())
                except (ValueError, TypeError, AttributeError):
                    log['error_type'] = 'RESPONSE_SCHEMA_UNKNOWN'
    except httpx.HTTPError as exc:
        log['error_type'] = type(exc).__name__  # No exception message/headers.
        log['response_headers_ms'] = None
    finally:
        log['request_elapsed_ms'] = round((time.perf_counter() - started) * 1000)
        log['tcp_connect_ms'] = round(sums['connect_tcp']) if 'connect_tcp' in seen else None
        log['tls_handshake_ms'] = round(sums['start_tls']) if 'start_tls' in seen else None
        log['network_events'] = events[-80:]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding='utf-8')
    # The public summary intentionally excludes IPs and error bodies (which might contain IPs).
    return {'label': label, 'request_sha256': log['request_sha256'], 'http_status': log['http_status'],
            'error_type': log['error_type'], 'tcp_connect_ms': log['tcp_connect_ms'],
            'tls_handshake_ms': log['tls_handshake_ms'], 'request_elapsed_ms': log['request_elapsed_ms'],
            'egress_ip_observed': log['egress'].get('ip') is not None,
            'log_saved_locally': True}


def compare_logs(first: dict, second: dict):
    if first['request_sha256'] != second['request_sha256']:
        raise ValueError('Requests differ; this is not a controlled network comparison')
    first_ip, second_ip = first.get('egress', {}).get('ip'), second.get('egress', {}).get('ip')
    different_ip = (first_ip != second_ip) if first_ip and second_ip else None
    one = re.sub(r'\s+', ' ', error_message(first.get('error_body') or '').strip().lower())
    two = re.sub(r'\s+', ' ', error_message(second.get('error_body') or '').strip().lower())
    same_route_error = (first['http_status'] == second['http_status'] == 400 and one == two
                        and 'failed to get config for provider' in one and 'not found' in one)
    if same_route_error:
        verdict = 'ROUTING_CONFIGURATION_PRIORITY'
    elif first['http_status'] != second['http_status'] or one != two or first.get('error_type') != second.get('error_type'):
        verdict = 'RESULTS_DIFFER_INVESTIGATE_NETWORK_AND_ACCESS_POLICY'
    else:
        verdict = 'SAME_RESULT_CAUSE_UNCONFIRMED'
    return {'same_request': True, 'egress_ips_different': different_ip,
            'same_provider_not_found': same_route_error, 'verdict': verdict,
            'credential_equality_verified': False,
            'caveat': 'Echo IP is not proof of gateway-observed IP; verify same credential and proxy policy locally.'}
