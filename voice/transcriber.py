"""Muse Voice Transcribe client over the Meta Model API.

Pipeline: arbitrary audio (webm/opus, ogg/opus) is converted to mono 16-bit PCM WAV at 24 kHz with the pip-bundled ffmpeg
binary (imageio-ffmpeg, no apt needed), then sent to
``POST {ASR_BASE_URL}/v1/asr/transcribe`` in PUSH_TO_TALK mode.

Billing: ~$0.18/hr of audio processed. Input is capped (VOICE_MAX_BYTES)
so a malicious upload cannot burn unbounded budget. Transcripts are
returned as plain data; nothing here touches escrow state.
"""
import logging
import subprocess
import uuid

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60
TARGET_RATE = 24000


class VoiceConfigError(Exception):
    """MODEL_API_KEY (or model config) missing, voice unavailable."""


class VoiceTranscriptionError(Exception):
    """Conversion or transcription failed; safe to relay to the user."""


def _ffmpeg_exe():
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise VoiceTranscriptionError(
            "Voice processing is not installed on this server."
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def convert_to_wav(audio_bytes):
    """Convert arbitrary audio to mono 16-bit PCM WAV at 24 kHz.

    Raises VoiceTranscriptionError on empty input, oversize input, or
    ffmpeg failure. Never logs audio content.
    """
    if not audio_bytes:
        raise VoiceTranscriptionError("No audio was received.")
    max_bytes = getattr(settings, 'VOICE_MAX_BYTES', 8 * 1024 * 1024)
    if len(audio_bytes) > max_bytes:
        raise VoiceTranscriptionError(
            "That recording is too long. Please keep voice notes under a minute.")
    try:
        proc = subprocess.run(
            [_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
             '-i', 'pipe:0', '-ac', '1', '-ar', str(TARGET_RATE),
             '-c:a', 'pcm_s16le', '-f', 'wav', 'pipe:1'],
            input=audio_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise VoiceTranscriptionError(
            "Voice processing failed. Please try again or type instead.") from exc
    if proc.returncode != 0 or not proc.stdout:
        logger.warning("ffmpeg conversion failed (rc=%s)", proc.returncode)
        raise VoiceTranscriptionError(
            "I could not understand that recording. Please try again or type instead.")
    return proc.stdout


def transcribe_wav(wav_bytes, timeout=None):
    """Send WAV bytes to Muse Voice Transcribe. Returns the raw JSON dict."""
    api_key = getattr(settings, 'MODEL_API_KEY', '')
    if not api_key:
        raise VoiceConfigError(
            "Voice transcription is not configured yet. Please type instead.")
    base_url = getattr(settings, 'ASR_BASE_URL', 'https://api.meta.ai').rstrip('/')
    model = getattr(settings, 'ASR_MODEL', 'muse-voice-transcribe-1.0')
    session_id = f"locktrans-{uuid.uuid4()}"
    request_part = {
        'mode': 'PUSH_TO_TALK',
        'model': model,
        'audioEncoding': 'WAV',
        'languageBias': ['English'],
        'keywords': ['Locktrans', 'escrow', 'PayPal', 'dollars',
                     'inspection', 'dispatch', 'payout', 'refund'],
    }
    import json as _json
    try:
        response = requests.post(
            f"{base_url}/v1/asr/transcribe?sessionId={session_id}",
            headers={'Authorization': f'Bearer {api_key}'},
            files={
                'request': ('request.json', _json.dumps(request_part),
                            'application/json'),
                'audio': ('audio.wav', wav_bytes, 'audio/wav'),
            },
            timeout=timeout or getattr(settings, 'VOICE_TIMEOUT', DEFAULT_TIMEOUT),
        )
    except requests.RequestException as exc:
        raise VoiceTranscriptionError(
            "Transcription timed out. Please try again or type instead.") from exc
    if response.status_code == 401:
        raise VoiceConfigError("Voice transcription rejected our credentials.")
    if response.status_code == 429:
        raise VoiceTranscriptionError(
            "Transcription is busy. Please try again in a moment.")
    if response.status_code not in (200, 201):
        logger.warning("Transcribe API %s: %s",
                       response.status_code, response.text[:300])
        raise VoiceTranscriptionError(
            "Transcription failed. Please try again or type instead.")
    try:
        return response.json()
    except ValueError as exc:
        raise VoiceTranscriptionError(
            "Transcription returned something unreadable.") from exc


def transcribe_audio(audio_bytes, timeout=None):
    """Full pipeline: convert then transcribe. Returns transcript string."""
    wav_bytes = convert_to_wav(audio_bytes)
    payload = transcribe_wav(wav_bytes, timeout=timeout)
    if not isinstance(payload, dict):
        raise VoiceTranscriptionError(
            "Transcription returned something unreadable.")
    transcript = (payload.get('transcript') or '').strip()
    if not transcript:
        raise VoiceTranscriptionError(
            "I didn't catch anything in that recording. Please try again or type.")
    logger.info("Voice transcribed %d chars", len(transcript))
    return transcript
