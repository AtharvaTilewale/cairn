"""Tests for the Meta model client: retries, usage log, contributor guard. No network."""

import json
from types import SimpleNamespace

import httpx2 as httpx
import openai
import pytest

from cairnlab.llm.client import Client, ContributorTierError, LLMError


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item))],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=50,
                prompt_tokens_details=SimpleNamespace(cached_tokens=40),
            ),
        )


def fake(script):
    comp = FakeCompletions(script)
    return SimpleNamespace(chat=SimpleNamespace(completions=comp)), comp


def status_error(code: int) -> openai.APIStatusError:
    req = httpx.Request("POST", "https://api.meta.ai/v1/chat/completions")
    resp = httpx.Response(code, request=req)
    cls = {429: openai.RateLimitError, 400: openai.BadRequestError}.get(
        code, openai.InternalServerError
    )
    return cls("boom", response=resp, body=None)


SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "string"}},
    "required": ["x"],
    "additionalProperties": False,
}


def test_structured_parses_and_logs_usage(tmp_path):
    oai, comp = fake(['{"x": "hi"}'])
    c = Client(home=tmp_path, openai_client=oai, sleep=lambda s: None)
    out = c.structured(
        purpose="test",
        messages=[{"role": "user", "content": "q"}],
        schema_name="T",
        schema=SCHEMA,
        reasoning_effort="low",
    )
    assert out == {"x": "hi"}
    call = comp.calls[0]
    assert call["model"] == "muse-spark-1.3"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["reasoning_effort"] == "low"
    (rec,) = [json.loads(x) for x in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert rec["purpose"] == "test" and rec["prompt_tokens"] == 100
    assert rec["cached_tokens"] == 40 and rec["completion_tokens"] == 50
    # 60 uncached in at $1.25/M + 40 cached at $0.15/M + 50 out at $4.25/M
    assert rec["cost_usd"] == pytest.approx((60 * 1.25 + 40 * 0.15 + 50 * 4.25) / 1e6)
    assert "key" not in json.dumps(rec).lower()


def test_retries_429_and_5xx_with_backoff(tmp_path):
    oai, comp = fake([status_error(429), status_error(503), '{"x": "ok"}'])
    waits = []
    c = Client(home=tmp_path, openai_client=oai, sleep=waits.append)
    assert c.structured(purpose="t", messages=[], schema_name="T", schema=SCHEMA) == {"x": "ok"}
    assert len(comp.calls) == 3 and len(waits) == 2 and waits[1] > waits[0] * 1.2


def test_never_retries_4xx(tmp_path):
    oai, comp = fake([status_error(400), '{"x": "never"}'])
    c = Client(home=tmp_path, openai_client=oai, sleep=lambda s: None)
    with pytest.raises(LLMError):
        c.structured(purpose="t", messages=[], schema_name="T", schema=SCHEMA)
    assert len(comp.calls) == 1


def test_gives_up_after_max_retries(tmp_path):
    oai, comp = fake([status_error(500)] * 10)
    c = Client(home=tmp_path, openai_client=oai, sleep=lambda s: None, max_retries=3)
    with pytest.raises(LLMError):
        c.structured(purpose="t", messages=[], schema_name="T", schema=SCHEMA)
    assert len(comp.calls) == 4


def test_bad_json_is_an_error(tmp_path):
    oai, _ = fake(["not json"])
    c = Client(home=tmp_path, openai_client=oai, sleep=lambda s: None)
    with pytest.raises(LLMError):
        c.structured(purpose="t", messages=[], schema_name="T", schema=SCHEMA)


def test_contributor_tier_refused_by_default(tmp_path):
    oai, _ = fake(['{"x": "y"}'])
    with pytest.raises(ContributorTierError):
        Client(home=tmp_path, model="muse-spark-1.3-contributor", openai_client=oai)
    Client(
        home=tmp_path, model="muse-spark-1.3-contributor", openai_client=oai, allow_contributor=True
    )


def test_glimmer_never_uses_temperature_zero(tmp_path):
    oai, comp = fake(['{"x": "y"}'])
    c = Client(home=tmp_path, backend="glimmer", openai_client=oai, sleep=lambda s: None)
    c.structured(purpose="t", messages=[], schema_name="T", schema=SCHEMA, temperature=0)
    call = comp.calls[0]
    assert call["model"] == "muse-glimmer" and call["temperature"] > 0
    assert "reasoning_effort" not in call  # mapped to the chat template instead


def test_missing_key_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LLMError, match="MODEL_API_KEY"):
        Client(home=tmp_path)


def test_glimmer_never_retries_a_timeout(tmp_path):
    """At ~2 tokens/s a retry regenerates from scratch for hours; fail instead."""
    from cairnlab.llm.client import GLIMMER_TIMEOUT_S

    req = httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")
    oai, comp = fake([openai.APITimeoutError(request=req), "never"])
    c = Client(home=tmp_path, backend="glimmer", openai_client=oai, sleep=lambda s: None)
    with pytest.raises(LLMError, match="Timeout"):
        c.chat(purpose="t", messages=[])
    assert len(comp.calls) == 1
    # Spark still retries timeouts (a dropped connection, not a slow model).
    oai, comp = fake([openai.APITimeoutError(request=req), "ok"])
    assert Client(openai_client=oai, sleep=lambda s: None).chat(purpose="t", messages=[]) == "ok"
    # Long enough for an overnight entry at the measured ~2 tokens/s.
    assert GLIMMER_TIMEOUT_S >= 4 * 3600
