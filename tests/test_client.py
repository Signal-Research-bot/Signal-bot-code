"""Tests for the Claude client wrapper.

The property that matters: there is no path to the API that skips the egress
firewall. Everything else here is the error handling the API actually requires
and that is easy to get wrong in ways only production reveals.

A fake transport is used throughout -- these tests make no network calls.
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.claude import schemas  # noqa: E402
from signal_research_bot.claude.client import (  # noqa: E402
    HAIKU,
    OPUS,
    SONNET,
    Client,
    Refusal,
    Usage,
    search_request,
    structured_request,
)
from signal_research_bot.egress import EgressViolation, Policy  # noqa: E402
from signal_research_bot.identity import Roster  # noqa: E402

FAKE_UUID = str(uuid.UUID(int=0xC0FFEE))


class Block:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class ServerToolUse:
    def __init__(self, n: int):
        self.web_search_requests = n


class UsageObj:
    def __init__(self, i: int, o: int, searches: int = 0):
        self.input_tokens = i
        self.output_tokens = o
        self.server_tool_use = ServerToolUse(searches) if searches else None


class Response:
    def __init__(self, text="ok", stop_reason="end_turn", details=None, usage=None):
        self.content = [Block(text)] if text is not None else []
        self.stop_reason = stop_reason
        self.stop_details = details
        self.usage = usage or UsageObj(10, 5)


@dataclass
class FakeTransport:
    response: Any = field(default_factory=Response)
    calls: list[dict] = field(default_factory=list)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def policy():
    return Policy.build(
        roster=Roster(names=("Anna Smith",), phones=(), group_name="Ravenhill"),
        allowed_labels={"Participant A", "Participant B"},
        group_id="Zm9v",
    )


@pytest.fixture
def client(policy, tmp_path):
    return Client(policy=policy, quarantine_dir=tmp_path, transport=FakeTransport())


def req(user: str = "Participant A: what is the reserve figure?") -> dict:
    return structured_request(
        model=SONNET, system="Extract tasks.", user=user, schema=schemas.EXTRACT
    )


# --- the firewall cannot be bypassed -----------------------------------------


def test_clean_request_reaches_the_transport(client):
    text, usage = client.send(**req())
    assert text == "ok" and usage.input_tokens == 10
    assert len(client.transport.calls) == 1


def test_request_containing_identity_never_reaches_the_transport(client):
    with pytest.raises(EgressViolation):
        client.send(**req("Anna Smith asked about reserves"))
    assert client.transport.calls == [], "payload was sent despite a violation"


def test_blocked_request_is_quarantined(client, tmp_path):
    with pytest.raises(EgressViolation):
        client.send(**req("Anna Smith asked"))
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_response_echoing_identity_is_blocked_before_return(policy, tmp_path):
    """The model can echo back what it was given, and this text is about to be
    written into a file other people read."""
    transport = FakeTransport(Response(text="Anna Smith asked about reserves"))
    c = Client(policy=policy, quarantine_dir=tmp_path, transport=transport)
    with pytest.raises(EgressViolation):
        c.send(**req())


def test_response_with_a_uuid_is_blocked(policy, tmp_path):
    transport = FakeTransport(Response(text=f"see {FAKE_UUID}"))
    c = Client(policy=policy, quarantine_dir=tmp_path, transport=transport)
    with pytest.raises(EgressViolation):
        c.send(**req())


# --- refusal handling ---------------------------------------------------------


class Details:
    def __init__(self, category):
        self.category = category
        self.explanation = "declined"


def test_refusal_with_empty_content_does_not_crash(policy, tmp_path):
    """A refusal is HTTP 200 with a possibly EMPTY content array. Reading
    content[0].text is the crash this ordering prevents."""
    transport = FakeTransport(
        Response(text=None, stop_reason="refusal", details=Details("cyber"))
    )
    c = Client(policy=policy, quarantine_dir=tmp_path, transport=transport)
    with pytest.raises(Refusal) as exc:
        c.send(**req())
    assert exc.value.category == "cyber"


def test_refusal_without_stop_details_does_not_crash(policy, tmp_path):
    """stop_details is populated only on refusal, and not always then."""
    transport = FakeTransport(Response(text=None, stop_reason="refusal", details=None))
    c = Client(policy=policy, quarantine_dir=tmp_path, transport=transport)
    with pytest.raises(Refusal) as exc:
        c.send(**req())
    assert exc.value.category is None


def test_normal_response_ignores_stop_details(client):
    client.transport.response = Response(text="fine", stop_reason="end_turn")
    assert client.send(**req())[0] == "fine"


# --- usage accounting ---------------------------------------------------------


def test_usage_accumulates_across_calls(client):
    client.send(**req())
    client.send(**req())
    assert client.usage.input_tokens == 20 and client.usage.output_tokens == 10


def test_search_requests_are_counted(policy, tmp_path):
    """Searches bill separately at a flat rate, so they are tracked separately."""
    transport = FakeTransport(Response(usage=UsageObj(100, 50, searches=3)))
    c = Client(policy=policy, quarantine_dir=tmp_path, transport=transport)
    c.send(**req())
    assert c.usage.searches == 3


def test_missing_usage_object_does_not_crash(policy, tmp_path):
    r = Response()
    r.usage = None
    c = Client(policy=policy, quarantine_dir=tmp_path, transport=FakeTransport(r))
    assert c.send(**req())[1] == Usage(0, 0, 0)


# --- structured output --------------------------------------------------------


def test_send_json_parses(policy, tmp_path):
    transport = FakeTransport(Response(text='{"tasks": []}'))
    c = Client(policy=policy, quarantine_dir=tmp_path, transport=transport)
    assert c.send_json(**req())[0] == {"tasks": []}


def test_invalid_json_raises_without_logging_the_body(policy, tmp_path):
    transport = FakeTransport(Response(text="not json at all, with content"))
    c = Client(policy=policy, quarantine_dir=tmp_path, transport=transport)
    with pytest.raises(ValueError) as exc:
        c.send_json(**req())
    assert "content" not in str(exc.value)


# --- request shapes -----------------------------------------------------------


def test_structured_request_disables_thinking_by_default():
    r = structured_request(model=SONNET, system="s", user="u", schema=schemas.EXTRACT)
    assert r["thinking"] == {"type": "disabled"}
    assert r["output_config"]["format"]["type"] == "json_schema"


def test_deep_research_request_has_no_output_format():
    """Citations and structured outputs conflict; stage 3 returns free text
    and a separate cheap call turns it into a record."""
    r = search_request(
        model=OPUS, system="s", user="u", max_uses=5,
        adaptive_thinking=True, with_fallbacks=True,
    )
    assert "format" not in r["output_config"]
    assert r["thinking"] == {"type": "adaptive"}
    assert r["fallbacks"] == "default" and r["_beta"] is True


def test_cheap_research_request_caps_searches_and_is_structured():
    r = search_request(
        model=HAIKU, system="s", user="u", max_uses=3, schema=schemas.CHEAP_RESEARCH
    )
    assert r["tools"][0]["max_uses"] == 3
    assert r["output_config"]["format"]["schema"] is schemas.CHEAP_RESEARCH


def test_cached_system_prompt_uses_one_hour_ttl():
    r = structured_request(
        model=SONNET, system="s", user="u", schema=schemas.TRIAGE, cache_system=True
    )
    assert r["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# --- schemas ------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [schemas.EXTRACT, schemas.TRIAGE, schemas.CHEAP_RESEARCH, schemas.KB_RECORD],
)
def test_schemas_satisfy_structured_output_constraints(schema):
    """Every object needs additionalProperties:false and a complete required
    list, or the API rejects it."""

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node["properties"])
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)

    check(schema)


def test_kb_record_requires_contradictions_and_open_questions():
    """Empty must mean 'looked and found none', never 'did not look'."""
    assert "contradictions" in schemas.KB_RECORD["required"]
    assert "open_questions" in schemas.KB_RECORD["required"]


def test_haiku_gets_the_search_tool_version_it_actually_accepts():
    """web_search_20260209 (dynamic filtering) is available on Opus 5/4.8/4.7/
    4.6, Fable 5, Sonnet 5 and Sonnet 4.6 -- not on Haiku 4.5, which is exactly
    what the cheap research stage uses. Sending it there is a 400 on every task,
    and because a BadRequestError is not a Refusal it was not caught by the
    per-task handler either: the window ended having written nothing, exit 0."""
    from signal_research_bot.claude import stages

    cheap = stages.cheap_research("q", "r")
    assert cheap["model"] == HAIKU
    assert cheap["tools"][0]["type"] == "web_search_20250305"
    assert "effort" not in cheap.get("output_config", {}), (
        "Haiku 4.5 does not accept output_config.effort"
    )


def test_opus_still_gets_dynamic_filtering_and_effort():
    from signal_research_bot.claude import stages

    deep = stages.deep_research("q", "why", "notes")
    assert deep["model"] == OPUS
    assert deep["tools"][0]["type"] == "web_search_20260209"
    assert deep["output_config"]["effort"] == "high"
