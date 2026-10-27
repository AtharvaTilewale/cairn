"""One client for Meta's models: ``muse-spark-1.3`` via the Meta Model API (``spark``)
or Muse Glimmer on a self-hosted OpenAI-compatible server (``glimmer``).

- The OpenAI SDK is used only as an HTTP client pointed at Meta's endpoint.
- ``MODEL_API_KEY`` comes from the environment or a ``.env`` file; it is never logged.
- The contributor tier (Meta may train on inputs) is refused unless explicitly allowed.
- 429 and 5xx are retried with exponential backoff and jitter; other 4xx never are.
- Every call's tokens, cost and latency are appended to ``<store>/usage.jsonl``.
"""

import json
import logging
import os
import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import openai
from dotenv import find_dotenv, load_dotenv

from cairnlab.store.models import utc_now

log = logging.getLogger(__name__)

META_BASE_URL = "https://api.meta.ai/v1"
SPARK_MODEL = "muse-spark-1.3"
GLIMMER_MODEL = "muse-glimmer"
GLIMMER_DEFAULT_URL = "http://127.0.0.1:8080/v1"  # llama-server; Ollama is :11434
GLIMMER_MIN_TEMPERATURE = 0.2  # Glimmer must never run at temperature 0
REQUEST_TIMEOUT_S = 120.0  # per attempt; the SDK default (10 min) hides a dead network
GLIMMER_TIMEOUT_S = 1800.0

# USD per million tokens: (input, cached input, output).
PRICES = {SPARK_MODEL: (1.25, 0.15, 4.25)}


class LLMError(RuntimeError):
    """A model call failed in a way the caller should handle (not a bug)."""


class ContributorTierError(LLMError):
    """The contributor tier lets Meta train on inputs; it needs explicit permission."""


def load_env() -> None:
    """Load ``.env`` from the current directory upwards, then ~/.config/cairn/env."""
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found, override=False)
    user_env = Path.home() / ".config" / "cairn" / "env"
    if user_env.is_file():
        load_dotenv(user_env, override=False)


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """The JSON object in a reply, tolerating reasoning text and code fences.

    Self-hosted servers without a reasoning parser can return ``<think>...</think>``
    before the answer, or wrap it in a ```json fence.
    """
    text = _THINK.sub("", text or "").strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                out, _ = decoder.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(out, dict):
                return out
    return None


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


class Client:
    """Chat and structured-output calls with retries and a usage log."""

    def __init__(
        self,
        home: Path | None = None,
        backend: str = "spark",
        model: str | None = None,
        allow_contributor: bool = False,
        openai_client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
    ):
        if backend not in ("spark", "glimmer"):
            raise ValueError(f"unknown backend {backend!r} (spark or glimmer)")
        self.backend = backend
        self.model = model or (
            SPARK_MODEL
            if backend == "spark"
            else os.environ.get("CAIRN_GLIMMER_MODEL", GLIMMER_MODEL)
        )
        if "contributor" in self.model and not allow_contributor:
            raise ContributorTierError(
                f"{self.model} lets Meta train on inputs; it is only for synthetic eval data"
            )
        self.home = home
        self.sleep = sleep
        self.max_retries = max_retries
        if openai_client is not None:
            self._oai = openai_client
        elif backend == "spark":
            load_env()
            key = os.environ.get("MODEL_API_KEY")
            if not key:
                raise LLMError("MODEL_API_KEY is not set (export it or put it in .env)")
            self._oai = openai.OpenAI(
                base_url=META_BASE_URL, api_key=key, max_retries=0, timeout=REQUEST_TIMEOUT_S
            )
        else:
            url = os.environ.get("CAIRN_GLIMMER_BASE_URL", GLIMMER_DEFAULT_URL)
            # Glimmer on a partially offloaded GPU is slow: allow long generations.
            self._oai = openai.OpenAI(
                base_url=url, api_key="not-needed", max_retries=0, timeout=GLIMMER_TIMEOUT_S
            )

    def chat(self, *, purpose: str, messages: list[dict], **kw: Any) -> str:
        """Plain completion; returns the reply text."""
        return self._call(purpose, messages, None, **kw)

    def structured(
        self,
        *,
        purpose: str,
        messages: list[dict],
        schema_name: str,
        schema: dict,
        **kw: Any,
    ) -> dict:
        """Completion constrained to a strict JSON schema; returns the parsed object."""
        fmt = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
        text = self._call(purpose, messages, fmt, **kw)
        out = extract_json(text)
        if out is None:
            raise LLMError(f"{purpose}: model did not return a JSON object")
        return out

    def complete(
        self,
        *,
        purpose: str,
        messages: list[dict],
        tools: list[dict],
        reasoning_effort: str | None = None,
    ) -> Any:
        """One tool-calling turn; returns the assistant message (``content``, ``tool_calls``)."""
        return self._call(purpose, messages, None, reasoning_effort=reasoning_effort, tools=tools)

    def _call(
        self,
        purpose: str,
        messages: list[dict],
        response_format: dict | None,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        tools: list[dict] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if response_format:
            kwargs["response_format"] = response_format
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.backend == "spark":
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort
            if temperature is not None:
                kwargs["temperature"] = temperature
        else:
            # Model card defaults: temperature 1.0, top_p 0.95, top_k 64. Reasoning cannot be
            # switched off; its amount is the chat template's reasoning_strength.
            kwargs["temperature"] = max(temperature or 1.0, GLIMMER_MIN_TEMPERATURE)
            kwargs["top_p"] = 0.95
            extra: dict[str, Any] = {"top_k": 64}
            if reasoning_effort in ("low", "medium", "high", "xhigh"):
                extra["chat_template_kwargs"] = {"reasoning_strength": reasoning_effort}
            elif reasoning_effort == "minimal":
                extra["chat_template_kwargs"] = {"reasoning_strength": "low"}
            kwargs["extra_body"] = extra
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                resp = self._oai.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - classified below
                if _retryable(exc) and attempt < self.max_retries:
                    wait = delay * (1 + random.random() * 0.5)
                    log.info("%s: %s, retrying in %.1fs", purpose, type(exc).__name__, wait)
                    self.sleep(wait)
                    delay *= 2
                    continue
                raise LLMError(f"{purpose}: {type(exc).__name__}: {exc}") from exc
            self._log_usage(purpose, resp, time.perf_counter() - t0)
            message = resp.choices[0].message
            return message if tools else message.content
        raise LLMError(f"{purpose}: gave up after {self.max_retries} retries")  # pragma: no cover

    def _log_usage(self, purpose: str, resp: Any, latency: float) -> None:
        usage = getattr(resp, "usage", None)
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        price_in, price_cached, price_out = PRICES.get(self.model, (0.0, 0.0, 0.0))
        cost = ((prompt - cached) * price_in + cached * price_cached + completion * price_out) / 1e6
        record = {
            "ts": utc_now(),
            "backend": self.backend,
            "model": self.model,
            "purpose": purpose,
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "completion_tokens": completion,
            "cost_usd": round(cost, 8),
            "latency_s": round(latency, 2),
        }
        if self.backend == "glimmer" and latency > 0:
            # Generation speed on our own hardware (includes prompt processing).
            record["tokens_per_s"] = round(completion / latency, 2)
        if self.home is None:
            return
        try:
            with open(self.home / "usage.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("could not write usage log: %s", exc)
