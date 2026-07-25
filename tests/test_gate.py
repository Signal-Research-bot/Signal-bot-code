"""Tests for the gate and grounding rules.

The gate is the cost model. These are cheap tests guarding an expensive
mistake: a threshold that silently lets everything through, or a cap that
truncates without telling anyone.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from signal_research_bot.gate import (  # noqa: E402
    allowed_urls,
    apply_gate,
    reject_ungrounded,
    should_escalate,
)


def task(q: str, worth: float = 0.9, difficulty: str = "low", duplicate=None,
         in_scope: bool = True) -> dict:
    return {
        "question": q,
        "in_scope": in_scope,
        "worth": worth,
        "difficulty": difficulty,
        "duplicate_of": duplicate,
        "rationale": "",
    }


# --- the gate ----------------------------------------------------------------


def test_low_worth_is_rejected():
    r = apply_gate([task("banter", worth=0.1)], worth_threshold=0.6, max_tasks=10)
    assert r.accepted == [] and len(r.rejected_low_worth) == 1


def test_threshold_is_inclusive():
    r = apply_gate([task("x", worth=0.6)], worth_threshold=0.6, max_tasks=10)
    assert len(r.accepted) == 1


def test_duplicates_are_rejected_before_worth():
    """A duplicate of an answered entry costs nothing to drop, however good."""
    r = apply_gate(
        [task("known", worth=1.0, duplicate="Research - known - 2026-07")],
        worth_threshold=0.6, max_tasks=10,
    )
    assert r.accepted == [] and len(r.rejected_duplicate) == 1


def test_cap_keeps_the_highest_worth():
    tasks = [task("a", 0.7), task("b", 0.95), task("c", 0.8)]
    r = apply_gate(tasks, worth_threshold=0.6, max_tasks=2)
    assert [t["question"] for t in r.accepted] == ["b", "c"]


def test_over_cap_tasks_are_deferred_not_discarded():
    """Silent truncation reads exactly like 'nothing was missed'."""
    r = apply_gate([task(x) for x in "abcde"], worth_threshold=0.6, max_tasks=2)
    assert len(r.deferred_over_cap) == 3
    assert r.counts["deferred_over_cap"] == 3


def test_gate_is_deterministic_for_idempotent_reruns():
    """A crashed batch is re-run; it must make the same decisions."""
    tasks = [task("a", 0.8), task("b", 0.8), task("c", 0.8)]
    first = apply_gate(tasks, worth_threshold=0.6, max_tasks=2)
    second = apply_gate(list(reversed(tasks)), worth_threshold=0.6, max_tasks=2)
    assert [t["question"] for t in first.accepted] == [
        t["question"] for t in second.accepted
    ]


def test_out_of_scope_is_rejected_before_worth_is_considered():
    """A high-worth lead about the wrong subject is still the wrong subject."""
    r = apply_gate([task("x", worth=1.0, in_scope=False)],
                   worth_threshold=0.6, max_tasks=10)
    assert r.accepted == [] and len(r.rejected_out_of_scope) == 1
    assert r.rejected_low_worth == [] and r.rejected_duplicate == []


def test_missing_in_scope_defaults_to_in_scope():
    """Older triage output without the field must not be silently discarded."""
    t = task("x"); del t["in_scope"]
    assert len(apply_gate([t], worth_threshold=0.6, max_tasks=10).accepted) == 1


def test_everything_is_accounted_for():
    """No task may vanish between triage and the report."""
    tasks = [task("a", 0.9), task("b", 0.1), task("c", 0.9, duplicate="x"),
             task("d", 0.9), task("e", 0.9, in_scope=False)]
    r = apply_gate(tasks, worth_threshold=0.6, max_tasks=1)
    assert sum(r.counts.values()) == len(tasks)


def test_zero_cap_blocks_everything():
    r = apply_gate([task("a")], worth_threshold=0.0, max_tasks=0)
    assert r.accepted == [] and len(r.deferred_over_cap) == 1


# --- escalation --------------------------------------------------------------


def resolved(**kw) -> dict:
    base = {
        "resolved": True,
        "answer": "yes",
        "confidence": "corroborated",
        "sources": [{"url": "https://a.example", "quote": "q"},
                    {"url": "https://b.example", "quote": "q"}],
        "escalation_reason": None,
    }
    base.update(kw)
    return base


def test_confident_multi_source_answer_does_not_escalate():
    escalate, _ = should_escalate(task("q"), resolved())
    assert escalate is False


def test_unresolved_escalates():
    escalate, why = should_escalate(task("q"), resolved(resolved=False))
    assert escalate and why


def test_high_difficulty_escalates_even_when_cheap_pass_is_confident():
    """Triage saw the conversation and the KB; the cheap pass saw neither."""
    escalate, why = should_escalate(task("q", difficulty="high"), resolved())
    assert escalate and "difficulty" in why


def test_single_source_confidence_escalates():
    escalate, _ = should_escalate(task("q"), resolved(confidence="single-source"))
    assert escalate


def test_unverified_confidence_escalates():
    escalate, _ = should_escalate(task("q"), resolved(confidence="unverified"))
    assert escalate


def test_thin_sourcing_escalates_unless_primary():
    one = [{"url": "https://a.example", "quote": "q"}]
    assert should_escalate(task("q"), resolved(sources=one))[0] is True
    # A single primary source is enough; that is what "primary" means.
    assert should_escalate(task("q"), resolved(sources=one, confidence="primary"))[0] is False


def test_missing_cheap_result_escalates():
    """A failed cheap pass must not silently drop the task."""
    escalate, why = should_escalate(task("q"), None)
    assert escalate and why


# --- grounding ---------------------------------------------------------------


def test_allowed_urls_collects_from_every_stage():
    a = {"sources": [{"url": "https://a.example"}]}
    b = {"sources": [{"url": "https://b.example"}]}
    assert allowed_urls(a, b, None) == {"https://a.example", "https://b.example"}


def test_fabricated_citation_is_rejected():
    """A plausible URL that was never retrieved is the failure mode this
    exists to prevent: it launders a guess into something that looks sourced."""
    record = {"evidence": [{"url": "https://invented.example", "quote": "q"}]}
    assert reject_ungrounded(record, {"https://real.example"}) == [
        "https://invented.example"
    ]


def test_grounded_citation_passes():
    record = {"evidence": [{"url": "https://real.example", "quote": "q"}]}
    assert reject_ungrounded(record, {"https://real.example"}) == []


def test_record_with_no_evidence_is_trivially_grounded():
    assert reject_ungrounded({"evidence": []}, set()) == []


# --- config validation --------------------------------------------------------


def test_short_group_id_is_rejected_with_a_useful_message(monkeypatch):
    """Regression found by an end-to-end dry run.

    The egress firewall checks for the group id by substring, which is right
    for a real 44-character id and catastrophic for a short one: "g" occurs in
    almost any text, so every batch was blocked with no obvious cause. The
    error now names the variable instead.
    """
    from signal_research_bot.config import Config, ConfigError

    monkeypatch.setenv("SRB_GROUP_ID", "g")
    with pytest.raises(ConfigError) as exc:
        Config.from_env()
    assert "SRB_GROUP_ID" in str(exc.value) and "too short" in str(exc.value)


def test_realistic_group_id_is_accepted(monkeypatch):
    from signal_research_bot.config import Config

    monkeypatch.setenv("SRB_GROUP_ID", "Zm9vYmFyZ3JvdXBpZGxvbmdlbm91Z2g9PQ==")
    assert Config.from_env().group_id.startswith("Zm9v")
