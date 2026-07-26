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
from signal_research_bot.egress import EgressViolation  # noqa: E402
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
    # What the SEARCH TOOL "returned", per stage. Deliberately separate from
    # the model's own `sources` array so a test can make them disagree -- which
    # is the whole point of the grounding control.
    search_results: dict[str, set] = field(default_factory=dict)
    last_retrieved_urls: set = field(default_factory=set)
    # The most recent outbound request. Needed to assert on what actually left
    # the machine -- a test that only checks the return value cannot tell
    # whether redaction ran.
    last_request: dict[str, Any] = field(default_factory=dict)

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
        self.last_request = request
        self.last_retrieved_urls = set(self.search_results.get(stage, set()))
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
        observed_handles_path=tmp_path / "observed-handles.json",
        auto_handles=True,
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
                         "format": record()},
                        search_results={"cheap": {"https://a.example"}})
    install(monkeypatch, client)
    assert batch_mod.run(cfg_for(env)) == 0
    assert "deep" not in client.calls, "Opus was called despite a confident cheap answer"
    written = next((env / "vault" / "Research Log").glob("*.md")).read_text(encoding="utf-8")
    # Asserting only that the file EXISTS is what this test used to do, and it
    # passed for the wrong reason: with no search_results the allowlist was
    # empty, every citation was stripped as ungrounded, and the page written was
    # a sourceless entry carrying the renderer's "_No sources were retrieved_"
    # line. The happy path has to prove a *grounded* write or it proves nothing.
    assert "a.example" in written
    assert "_No sources were retrieved" not in written


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
    client = FakeClient(
        {"extract": extracted(), "triage": triaged(),
         "cheap": cheap(urls=("https://real.example", "https://also-real.example")),
         "format": record(urls=("https://real.example", "https://invented.example"))},
        search_results={"cheap": {"https://real.example", "https://also-real.example"}},
    )
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
                         "format": record()},
                        search_results={"cheap": {"https://a.example"}})
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


# --- grounding: the allowlist must come from the search tool -------------------


def test_a_url_the_model_invented_is_rejected(env, monkeypatch):
    """The control's whole purpose, and it was circular until an audit.

    The model's own `sources` array claims a URL the search tool never
    returned. If the allowlist were built from that array it would validate
    itself. It must be built from the search results instead.
    """
    client = FakeClient(
        {"extract": extracted(), "triage": triaged(),
         "cheap": cheap(urls=("https://invented.example", "https://also-fake.example")),
         "format": record(urls=("https://invented.example",))},
        search_results={"cheap": {"https://real.example"}},
    )
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    # EVERY citation was invented, so stripping them leaves no evidence at all.
    # The entry is not written: notify.py tells the group that every answer
    # carries its sources, and a page whose evidence table says "_No sources
    # were retrieved_" reads like a finding rather than a failed lookup.
    assert not list((env / "vault" / "Research Log").glob("*.md"))


def test_a_url_the_search_returned_is_kept(env, monkeypatch):
    client = FakeClient(
        {"extract": extracted(), "triage": triaged(),
         "cheap": cheap(urls=("https://real.example", "https://real2.example")),
         "format": record(urls=("https://real.example",))},
        search_results={"cheap": {"https://real.example", "https://real2.example"}},
    )
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    written = next((env / "vault" / "Research Log").glob("*.md")).read_text(encoding="utf-8")
    assert "real.example" in written


def test_escalated_tasks_keep_their_own_citations(env, monkeypatch):
    """Regression: `allowed` was built only from the cheap pass, so every
    source found by the deep stage was stripped from the written entry."""
    client = FakeClient(
        {"extract": extracted(), "triage": triaged(),
         "cheap": cheap(resolved=False), "deep": "deep text",
         "format": record(urls=("https://found-by-opus.example",))},
        search_results={"cheap": {"https://cheap-only.example"},
                        "deep": {"https://found-by-opus.example"}},
    )
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    written = next((env / "vault" / "Research Log").glob("*.md")).read_text(encoding="utf-8")
    assert "found-by-opus.example" in written, "the deep stage's own sources were stripped"


# --- audit regressions --------------------------------------------------------


def test_a_firewall_block_on_stage_one_does_not_wedge_every_future_window(
    env, monkeypatch
):
    """The stage-1 and stage-2 calls carried the whole window and sat outside
    any try/except, and main() did not catch EgressViolation either.

    The failure was not the crash. It was that messages stay pending on a crash
    by design, so the next run rebuilt the identical payload, and the firewall
    rejected it identically -- forever. One redaction miss would have stopped
    the bot permanently and silently, with every later message queued behind it.
    """
    class Blocking(FakeClient):
        def send_json(self, **request):
            raise EgressViolation("roster-name", "planted", "abc123")

    install(monkeypatch, Blocking({}))
    cfg = cfg_for(env)
    assert batch_mod.run(cfg) == 1

    metrics = (env / "metrics.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert '"window_blocked": "roster-name"' in metrics[0]

    # The window must be CONSUMED, not left pending. A second run must have
    # nothing to do rather than replay the same rejected payload.
    ok = FakeClient({"extract": {"tasks": []}})
    install(monkeypatch, ok)
    assert batch_mod.run(cfg_for(env)) == 0
    assert "extract" not in ok.calls, "the blocked window was replayed"


def test_a_refusal_before_triage_leaves_the_window_pending(env, monkeypatch):
    """The counterweight to the test above. A refusal is transient in a way a
    firewall block is not, so those messages must NOT be thrown away."""
    class Refusing(FakeClient):
        def send_json(self, **request):
            raise Refusal("cyber", "no")

    install(monkeypatch, Refusing({}))
    assert batch_mod.run(cfg_for(env)) == 1

    replayed = FakeClient({"extract": {"tasks": []}})
    install(monkeypatch, replayed)
    assert batch_mod.run(cfg_for(env)) == 0
    assert "extract" in replayed.calls, "a transient failure discarded the window"


def test_an_entry_with_no_grounded_sources_is_not_written(env, monkeypatch):
    """notify.py tells the group every answer carries its sources. When every
    citation is stripped as ungrounded the renderer has a dedicated string for
    the empty case, which reads like a finding rather than a failed lookup."""
    client = FakeClient(
        {"extract": extracted(), "triage": triaged(),
         "cheap": cheap(urls=("https://invented.example",)),
         "format": record(urls=("https://invented.example",))},
        search_results={"cheap": {"https://real.example"}},
    )
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    assert not list((env / "vault" / "Research Log").glob("*.md"))
    line = (env / "metrics.jsonl").read_text(encoding="utf-8")
    assert '"unsourced_dropped": 1' in line
    assert '"written": 0' in line


def test_a_deferred_task_is_recorded_not_just_counted(env, monkeypatch):
    """The window is marked processed at the end of the run, so a task deferred
    by the cap was never seen again -- the only trace a question ever existed
    was an integer in a log line."""
    client = FakeClient(
        {"extract": extracted(n=3), "triage": triaged(n=3),
         "cheap": cheap(), "format": record()},
        search_results={"cheap": {"https://a.example"}},
    )
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env, max_tasks_per_window=1))
    line = (env / "metrics.jsonl").read_text(encoding="utf-8")
    assert '"deferred_over_cap": 2' in line
    assert '"deferred_questions"' in line
    assert "q1" in line and "q2" in line


def test_a_failed_task_leaves_its_question_behind(env, monkeypatch):
    client = FakeClient(
        {"extract": extracted(), "triage": triaged(), "cheap": cheap(),
         "format": ValueError("bad json")},
        search_results={"cheap": {"https://a.example"}},
    )
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    line = (env / "metrics.jsonl").read_text(encoding="utf-8")
    assert '"unfinished_questions": ["q0"]' in line


# --- learning handles by watching --------------------------------------------


def _observed(env, *names):
    (env / "observed-handles.json").write_text(
        json.dumps({"observed": list(names)}), encoding="utf-8"
    )


def test_an_observed_handle_is_redacted_without_the_operator_doing_anything(
    env, monkeypatch
):
    """The whole point: in a pseudonymous group the operator cannot supply the
    deny-list, so the receiver watches and the batch uses what it saw."""
    _observed(env, "zeropoint_x")
    cache = Cache.open(env / "c.db", None, allow_plaintext=True)
    ts = TS + 10 * 60_000
    cache.add(ParsedMessage(
        kind=Kind.MESSAGE, group_id=GROUP, source=ALICE,
        timestamp_ms=ts - (ts % (15 * 60 * 1000)),
        body="zeropoint_x says the attestation is fine", raw_timestamp_ms=ts,
    ))
    cache.close()

    client = FakeClient({"extract": {"tasks": []}})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))

    sent = str(client.last_request)
    assert "zeropoint_x" not in sent
    assert "[participant]" in sent


def test_a_handle_that_is_really_an_ordinary_word_is_not_used(env, monkeypatch):
    """A display name is attacker-controlled and, more often, simply unlucky.
    Someone calling themselves "reserves" would otherwise have that word
    redacted out of every page -- silently, looking like the research just came
    up empty. Frequency is the discriminator, so no word list has to be kept."""
    _observed(env, "reserves")
    cache = Cache.open(env / "c.db", None, allow_plaintext=True)
    for i in range(14):
        ts = TS + (i + 5) * 60_000
        cache.add(ParsedMessage(
            kind=Kind.MESSAGE, group_id=GROUP, source=ALICE,
            timestamp_ms=ts - (ts % (15 * 60 * 1000)),
            body=f"the reserves report {i} looks wrong", raw_timestamp_ms=ts,
        ))
    cache.close()

    client = FakeClient({"extract": {"tasks": []}})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))

    assert "reserves" in str(client.last_request), (
        "the subject matter was redacted out of the research"
    )
    line = (env / "metrics.jsonl").read_text(encoding="utf-8")
    assert '"auto_handles_rejected": 1' in line


def test_a_hand_listed_handle_is_never_vetted(env, monkeypatch):
    """A human chose it, which beats any heuristic here. If the operator says
    a member is called "reserves", that is the operator's call to make."""
    (env / "roster.json").write_text(
        json.dumps({"handles": ["reserves"], "group_name": "Ravenhill"}),
        encoding="utf-8",
    )
    cache = Cache.open(env / "c.db", None, allow_plaintext=True)
    for i in range(14):
        ts = TS + (i + 5) * 60_000
        cache.add(ParsedMessage(
            kind=Kind.MESSAGE, group_id=GROUP, source=ALICE,
            timestamp_ms=ts - (ts % (15 * 60 * 1000)),
            body=f"the reserves report {i}", raw_timestamp_ms=ts,
        ))
    cache.close()

    client = FakeClient({"extract": {"tasks": []}})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env))
    assert "reserves" not in str(client.last_request)


def test_auto_handles_can_be_switched_off(env, monkeypatch):
    _observed(env, "zeropoint_x")
    cache = Cache.open(env / "c.db", None, allow_plaintext=True)
    ts = TS + 10 * 60_000
    cache.add(ParsedMessage(
        kind=Kind.MESSAGE, group_id=GROUP, source=ALICE,
        timestamp_ms=ts - (ts % (15 * 60 * 1000)),
        body="zeropoint_x said so", raw_timestamp_ms=ts,
    ))
    cache.close()

    client = FakeClient({"extract": {"tasks": []}})
    install(monkeypatch, client)
    batch_mod.run(cfg_for(env, auto_handles=False))
    assert "zeropoint_x" in str(client.last_request)
