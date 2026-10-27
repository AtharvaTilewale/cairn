"""Speech to text with ``muse-voice-transcribe-1.0`` (Meta Model API).

Uses the file endpoint, verified 2026-10-27: ``POST /v1/asr/transcribe``,
multipart/form-data with a ``request`` part (JSON ``{"model", "audioEncoding"}``,
``"WAV"`` accepted) and an ``audio`` part. The response is
``{"sessionId", "transcript", "audioDurationMs", "turns"}``. Short push-to-talk notes
do not need the realtime WebSocket (its handshake is not documented; see CLAUDE.md).
"""

import json
import logging
import os
import random
import time
from collections.abc import Callable
from pathlib import Path

import httpx2 as httpx

from cairnlab.llm.client import META_BASE_URL, LLMError, load_env
from cairnlab.store.models import utc_now

log = logging.getLogger(__name__)

ASR_MODEL = "muse-voice-transcribe-1.0"
PRICE_PER_AUDIO_HOUR = 0.18


def transcribe_wav(
    wav: bytes,
    home: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = 4,
) -> str:
    """Transcript of a WAV file; retries 429/5xx, logs duration and cost to usage.jsonl."""
    load_env()
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise LLMError("MODEL_API_KEY is not set (export it or put it in .env)")
    request = json.dumps({"model": ASR_MODEL, "audioEncoding": "WAV"})
    delay = 1.0
    with httpx.Client(timeout=120, transport=transport) as client:
        for attempt in range(max_retries + 1):
            t0 = time.perf_counter()
            try:
                resp = client.post(
                    f"{META_BASE_URL}/asr/transcribe",
                    headers={"Authorization": f"Bearer {key}"},
                    files={
                        "request": (None, request, "application/json"),
                        "audio": ("note.wav", wav, "audio/wav"),
                    },
                )
            except httpx.TransportError as exc:
                if attempt < max_retries:
                    sleep(delay * (1 + random.random() * 0.5))
                    delay *= 2
                    continue
                raise LLMError(f"transcribe: {type(exc).__name__}: {exc}") from exc
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if retryable and attempt < max_retries:
                sleep(delay * (1 + random.random() * 0.5))
                delay *= 2
                continue
            if resp.status_code != 200:
                try:
                    message = resp.json().get("error", {}).get("message", resp.text[:200])
                except ValueError:
                    message = resp.text[:200]
                raise LLMError(f"transcribe: HTTP {resp.status_code}: {message}")
            body = resp.json()
            _log_usage(home, body.get("audioDurationMs") or 0, time.perf_counter() - t0)
            return (body.get("transcript") or "").strip()
    raise LLMError("transcribe: gave up")  # pragma: no cover


def _log_usage(home: Path | None, duration_ms: int, latency: float) -> None:
    if home is None:
        return
    record = {
        "ts": utc_now(),
        "backend": "spark",
        "model": ASR_MODEL,
        "purpose": "voice_note",
        "audio_seconds": round(duration_ms / 1000, 2),
        "cost_usd": round(duration_ms / 3_600_000 * PRICE_PER_AUDIO_HOUR, 8),
        "latency_s": round(latency, 2),
    }
    try:
        with open(home / "usage.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        log.warning("could not write usage log: %s", exc)
