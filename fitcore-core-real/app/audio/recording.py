"""Local microphone capture. No network calls and no automatic recording on import."""
import math
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

TARGET_RATE = 16000


def pcm16_mono(samples, source_rate: int) -> np.ndarray:
    if source_rate < 8000 or source_rate > 192000:
        raise ValueError('Unsupported source sample rate')
    signal = np.asarray(samples, dtype=np.float64)
    if signal.ndim == 2:
        signal = signal.mean(axis=1)
    if signal.ndim != 1 or signal.size == 0 or not np.isfinite(signal).all():
        raise ValueError('Audio is empty or invalid')
    if source_rate != TARGET_RATE:
        ratio = Fraction(TARGET_RATE, source_rate)
        signal = resample_poly(signal, ratio.numerator, ratio.denominator)
    return np.rint(np.clip(signal, -1, 1) * 32767).astype('<i2')


def write_wav(path: Path, pcm: np.ndarray):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(TARGET_RATE)
        output.writeframes(np.asarray(pcm, dtype='<i2').tobytes())


def validate_wav(path: Path) -> dict:
    with wave.open(str(path), 'rb') as wav:
        rate, channels, width, frames = wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()
        if (rate, channels, width, wav.getcomptype()) != (TARGET_RATE, 1, 2, 'NONE'):
            raise ValueError('WAV must be 16kHz mono 16-bit uncompressed PCM')
        raw = wav.readframes(frames)
    if frames == 0 or len(raw) != frames * channels * width:
        raise ValueError('WAV has no samples or is truncated')
    samples = np.frombuffer(raw, dtype='<i2').astype(np.float64) / 32768
    # Remove DC offset; a constant voltage must not pass as non-silent audio.
    centered = samples - samples.mean()
    rms = float(np.sqrt(np.mean(centered ** 2)))
    peak = float(np.max(np.abs(centered)))
    if rms < 1e-4 or peak < 1e-3:
        raise ValueError('Audio is silent or too quiet; check microphone and record again')
    return {'sample_rate': rate, 'channels': channels, 'bits_per_sample': width * 8,
            'frames': frames, 'duration_seconds': round(frames / rate, 3),
            'rms_dbfs': round(20 * math.log10(rms), 2),
            'peak_dbfs': round(20 * math.log10(peak), 2),
            'clipped_fraction': round(float(np.mean(np.abs(samples) >= .999)), 6),
            'non_silent': True}


def input_configuration(sounddevice, device=None):
    info = sounddevice.query_devices(device, 'input')
    channels = int(info['max_input_channels'])
    if channels < 1:
        raise ValueError('Selected device has no microphone input')
    native_rate = round(float(info['default_samplerate']))
    for rate, count in dict.fromkeys([(TARGET_RATE, 1), (native_rate, 1), (native_rate, min(channels, 2))]):
        try:
            sounddevice.check_input_settings(device=device, channels=count, dtype='float32', samplerate=rate)
            return rate, count
        except sounddevice.PortAudioError:
            continue
    raise ValueError('Microphone does not support the tested recording configurations')


def capture(seconds: float, device=None) -> tuple[np.ndarray, dict]:
    import sounddevice as sd
    if not 1 <= seconds <= 30:
        raise ValueError('Recording duration must be between 1 and 30 seconds')
    rate, channels = input_configuration(sd, device)
    samples = sd.rec(round(seconds * rate), samplerate=rate, channels=channels,
                     dtype='float32', device=device)
    sd.wait()
    status = sd.get_status()
    if status:
        raise RuntimeError('Recording suffered an audio overflow; close other audio apps and retry')
    return pcm16_mono(samples, rate), {'recorded_sample_rate': rate,
                                     'recorded_channels': channels, 'resampled': rate != TARGET_RATE}
