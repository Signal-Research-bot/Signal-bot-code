"""Tests for the batch orchestrator.

Everything is faked at the Client boundary, so no network call happens. The
properties under test are the orchestration ones that only appear when the
pieces are wired together: one bad task must not abort the window, escalation
must route correctly, ungrounded citations must be dropped, and a crash must
leave messages pending rather than silently consumed.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot import batch as batch_mod  # noqa: E402
from signal_research_bot.cache import Cache  # noqa: E402
from signal_research_bot.claude.client import Refusal, Usage  # noqa: E402
from signal_research_bot.config import Config  # noqa: E402
from signal_research_bot.envelope import Kind, ParsedMessage  # noqa: E402

ALICE = str(uuid.UUID(int=0xA11CE))
GROUP = "Zm9vYmFyZ3JvdXBpZGxvbmdlbm91Z2g9PQ=="
TS = 1_784_000_000_000

# (distinctive phrase, stage). Order matters: first match wins.
STAGE_MARKERS = [
    ("RESEARCHABLE LEADS", "extract"),
    ("gate in front", "triage"),
    ("Answer the question from primary sources", "cheap"),
    ("investigative researcher", "deep"),
]


def test_stage_markers_are_not_stale():
    """Guards the test helper itself.

    If a prompt is reworded and its marker is not, every request falls through
    to "format" and the orchestration tests pass while asserting nothing.
    """
    from signal_research_bot.claude import stages as st
    prompts = {
        "extract": st.EXTRACT_SYSTEM, "triage": st.TRIAGE_SYSTEM,
        "cheap": st.CHEAP_SYSTEM, "deep": st.DEEP_SYSTEM,
    }
    for marker, stage in STAGE_MARKERS:
        assert marker in prompts[stage], (
            f"marker {marker!r} no longer appears in the {stage} prompt"
        )


@dataclass
class FakeClient:
    """Returns queued responses in order, keyed by which stage asked."""

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    def _stage_of(self, request: dict) -> str:
        """Identify the stage from its prompt.

        Matches on a distinctive phrase per stage. Brittle by nature -- reword
        a prompt and this silently misroutes everything to "format" -- so
        _assert_markers_present() below fails loudly if a marker goes stale
        rather than letting the suite pass while testing nothing.
        """
        system = str(request.get("system", ""))
        for marker, stage in STAGE_MARKERS:
            if marker in system:
                return stage
        return "format"

    def _resolve(self, request: dict):
        stage = self._stage_of(request)
        self.calls.append(stage)
        value = self.responses.get(stage)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(len([c for c in self.calls if c == stage]))
        return value

    def send(self, **request):
        return self._resolve(request), Usage()

    def send_json(self, **request):
        return self._resolve(request), Usage()


def cfg_for(tmp_path: Path, **kw) -> Config:
    base = dict(
        signal_host="h", signal_port=1, group_id=GROUP,
        cache_path=tmp_path / "c.db", cache_key=None,
        roster_path=tmp_path / "roster.json",
        pseudonyms_path=tmp_path / "p.json",
        quarantine_dir=tmp_path / "q",
        metrics_path=tmp_path / "metrics.jsonl",
        kb_dir=tmp_path / "vault", foreign_vault_dir=None,
        log_level="CRITICAL", max_tasks_per_window=4, worth_threshold=0.6,
        notify=False,
        research_domain='Financial investigation of the crypto sector.',
    )
    base.update(kw)
    return Config(**base)


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "vault").mkdir()
    (tmp_path / "roster.json").write_text(
        json.dumps({"names": ["Anna Smith"], "phones": [], "group_name": "Ravenhill"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SRB_PSEUDONYM_KEY", "ab" * 32)

    # run() opens the cache itself and (correctly) demands encryption. Capture
    # the real classmethod BEFORE patching, or the shim calls itself.
    real_open = Cache.open
    monkeypatch.setattr(
        Cache,
        "open",
        staticmethod(lambda path, key=None, **kw: real_open(path, key, allow_plaintext=True)),
    )
    cache = Cache.open(tmp_path / "c.db")
    for i, body in enumerate(["is the attestation an audit?", "what about reserves?"]):
        ts = TS + i * 60_000
        cache.add(ParsedMessage(
            kind=Kind.MESSAGE, group_id=GROUP, source=ALICE,
            timestamp_ms=ts - (ts % (15 * 60 * 1000)), body=body, raw_timestamp_ms=ts,
        ))
    cache.close()
    return tmp_path


def install(monkeypatch, client: FakeClient) -> None:
    monkeypatch.setattr(batch_mod, "Client", lambda **kw: client)
    monkeypatch.setattr(batch_mod, "AnthropicTransport", lambda *a, **k: None)


def extracted(n=1):
    return {"tasks": [{"question": f"q{i}", "raised_by": "Participant A",
                       "context": "c", "kind": "factual"} for i in range(n)]}


def triaged(n=1, worth=0.9, difficulty="low", in_scope=True):
    return {"tasks": [{"question": f"q{i}", "in_scope": in_scope, "worth": worth,
                       "difficulty": difficulty, "duplicate_of": None,
                       "rationale": "r"} for i in range(n)]}


def cheap(resolved=True, urls=("https://a.example",)):
    return {"resolved": resolved, "answer": "an answer", "confidence": "corroborated",
            "sources": [{"url": u, "quote": "q"} for u in urls],
            "escalation_reason": None}


def record(urls=("https://a.example",)):
    return {"title": "Research - q0 - 2026-07", "question": "q0", "answer": "a",
            "confidence": "corroborated", "research_status": "answered",
            "evidence": [{"url": u, "quote": "q", "confidence": "primary"} for u in urls],
            "contradictions": [], "open_questions": [], "tags": []}


# --- the happy path -----------------------------------------------------------


def test_resolved_task_skips_the_expensive_model(env, monkeypatch):
    client = FakeClient({"extract": extracted(), "triage": triaged(),
                         "cheap": cheap(urls=("https://a.example", "https://b.example")),
                         "format": record()})
    install(monkeypatch, client)
    assert batch_mod.run(cfg_for(env)) == 0
    assert "deep" not in client.calls, "Opus was called despite a confident cheap answer"
    assert list((env / "vault" / "Research Log").glob("*.md"))


def test_unresolved_task_escalates(env, monkeypatch):
    client = FakeClient({"extract": extracted(), "triage": triaged(),
                         "cheap": cheap(resolved=False), "deep": "deep research text",
                         "format": record()})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    assert "deep" in client.calls


def test_high_difficulty_escalates_even_when_cheap_is_confident(env, monkeypatch):
    client = FakeClient({"extract": extracted(), "triage": triaged(difficulty="high"),
                         "cheap": cheap(), "deep": "text", "format": record()})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    assert "deep" in client.calls


# --- the gate -----------------------------------------------------------------


def test_low_worth_tasks_never_reach_a_research_stage(env, monkeypatch):
    client = FakeClient({"extract": extracted(), "triage": triaged(worth=0.1)})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    assert "cheap" not in client.calls and "deep" not in client.calls


def test_cap_limits_expensive_calls(env, monkeypatch):
    client = FakeClient({"extract": extracted(5), "triage": triaged(5),
                         "cheap": cheap(urls=("https://a.example", "https://b.example")),
                         "format": record()})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env, max_tasks_per_window=2))
    assert client.calls.count("cheap") == 2


def test_empty_extraction_ends_the_window_cleanly(env, monkeypatch):
    client = FakeClient({"extract": {"tasks": []}})
    install(monkeypatch, client)
    assert batch_mod.run(cfg_for(env)) == 0
    assert "triage" not in client.calls


# --- failure policy -----------------------------------------------------------


def test_one_failing_task_does_not_abort_the_window(env, monkeypatch):
    """One bad question must not cost the other nineteen."""
    calls = {"n": 0}

    def flaky_cheap(_n):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad json")
        return cheap(urls=("https://a.example", "https://b.example"))

    client = FakeClient({"extract": extracted(2), "triage": triaged(2),
                         "cheap": flaky_cheap, "deep": "text", "format": record()})
    install(monkeypatch, client)
    assert batch_mod.run(cfg_for(env)) == 0
    assert client.calls.count("format") >= 1, "the second task was not processed"


def test_a_refusal_is_counted_not_fatal(env, monkeypatch):
    client = FakeClient({"extract": extracted(), "triage": triaged(),
                         "cheap": Refusal("cyber", "no")})
    install(monkeypatch, client)
    assert batch_mod.run(cfg_for(env)) == 0


# --- grounding ----------------------------------------------------------------


def test_ungrounded_citations_are_dropped_from_the_record(env, monkeypatch):
    """A URL the search never returned is a fabrication, and a fabricated
    citation in a sourced archive is worse than no entry."""
    client = FakeClient({
        "extract": extracted(), "triage": triaged(),
        "cheap": cheap(urls=("https://real.example", "https://also-real.example")),
        "format": record(urls=("https://real.example", "https://invented.example")),
    })
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    written = next((env / "vault" / "Research Log").glob("*.md")).read_text(encoding="utf-8")
    assert "real.example" in written
    assert "invented.example" not in written


# --- bookkeeping --------------------------------------------------------------


def test_messages_are_marked_processed_and_not_reprocessed(env, monkeypatch):
    client = FakeClient({"extract": extracted(), "triage": triaged(),
                         "cheap": cheap(urls=("https://a.example", "https://b.example")),
                         "format": record()})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))

    second = FakeClient({"extract": extracted()})
    install(monkeypatch, second)
    batch_mod.run(cfg_for(env))
    assert second.calls == [], "a second run reprocessed already-handled messages"


def test_metrics_are_written_and_carry_no_content(env, monkeypatch):
    client = FakeClient({"extract": extracted(), "triage": triaged(),
                         "cheap": cheap(urls=("https://a.example", "https://b.example")),
                         "format": record()})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    line = (env / "metrics.jsonl").read_text(encoding="utf-8").strip()
    assert '"written": 1' in line
    assert "attestation" not in line and "Participant" not in line


def test_empty_window_writes_nothing(env, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "vault").mkdir()
    (empty / "roster.json").write_text(json.dumps({"names": ["X Y"]}), encoding="utf-8")
    client = FakeClient({})
    install(monkeypatch, client)
    assert batch_mod.run(cfg_for(empty)) == 0
    assert client.calls == []


# --- scope gating --------------------------------------------------------------


def test_out_of_scope_leads_never_reach_a_research_stage(env, monkeypatch):
    """The archive has a subject. A fascinating question about the wrong one
    still costs money and clutters the vault."""
    client = FakeClient({"extract": extracted(), "triage": triaged(in_scope=False)})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    assert "cheap" not in client.calls and "deep" not in client.calls


def test_the_domain_actually_reaches_the_model(env, monkeypatch):
    """A scope setting nothing is told about is not a scope."""
    seen = {}
    class Spy(FakeClient):
        def send_json(self, **request):
            seen.setdefault(self._stage_of(request), request.get("system", ""))
            return super().send_json(**request)
    client = Spy({"extract": {"tasks": []}})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env, research_domain="UNIQUE-DOMAIN-MARKER"))
    assert "UNIQUE-DOMAIN-MARKER" in seen.get("extract", "")
