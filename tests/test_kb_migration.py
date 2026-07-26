"""Tests for adopting pages written before the topic index existed.

The live vault already holds pages whose filenames are wikilink targets other
notes point at. The single property that matters here is that adoption does not
move, rewrite or reformat any of them.

Every fixture is synthetic. `.claude/skills/privacy-invariants` forbids test
data captured from a real run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.kb.adopt import adopt, plan  # noqa: E402
from signal_research_bot.kb.state import VaultIndex, content_hash  # noqa: E402

# Shaped like the pages actually in the vault: an apostrophe in the title, a
# stripped slash in the filename, and no topic_key.
LEGACY = """---
title: "Tether's equity stake in Anchorage Digital"
entity_type: research_task
research_status: answered
finding: supported
confidence: corroborated
first_raised: 2026-07-26
last_verified: 2026-07-26
tags: ["research", "signal-derived", "tether"]
sources: ["https://a.example/1"]
related: []
---

# Tether's equity stake in Anchorage Digital

**A stake was made.**

## Question

Did it happen?

## Provenance

Raised in the group chat and researched automatically.
"""


@pytest.fixture
def research_log(tmp_path):
    d = tmp_path / "Research Log"
    d.mkdir(parents=True)
    (d / "Tether's equity stake in Anchorage Digital.md").write_text(
        LEGACY, encoding="utf-8"
    )
    (d / "Did Tether get a regulatory pass or bailout from TreasuryFed.md").write_text(
        LEGACY, encoding="utf-8"
    )
    return d


def test_a_dry_run_writes_absolutely_nothing(research_log):
    before = {p: p.read_text(encoding="utf-8") for p in research_log.rglob("*")}
    adopt(research_log, dry_run=True)
    after = {p: p.read_text(encoding="utf-8") for p in research_log.rglob("*")}
    assert before == after
    assert not (research_log / ".srb-state").exists()


def test_adoption_never_renames_or_rewrites_a_page(research_log):
    """The acceptance test for the migration: a page's filename is its wikilink
    target, and its prose is not something the bot has any record of."""
    before = {p.name: p.read_text(encoding="utf-8") for p in research_log.glob("*.md")}
    adopt(research_log)
    after = {p.name: p.read_text(encoding="utf-8") for p in research_log.glob("*.md")}
    assert before == after


def test_every_adopted_page_gets_a_topic_key(research_log):
    adopt(research_log)
    index = VaultIndex(research_log).load()
    assert len(index.keys()) == 2
    assert all(k and k.islower() for k in index.keys())


def test_an_adopted_page_keeps_the_stem_it_already_had(research_log):
    adopt(research_log)
    stems = VaultIndex(research_log).load().stems()
    assert "Tether's equity stake in Anchorage Digital" in stems


def test_an_adopted_page_is_marked_unmanaged(research_log):
    """The bot has no record of what is in it, so it must never re-render it."""
    adopt(research_log)
    assert all(not s.managed for s in VaultIndex(research_log).load().all())


def test_an_adopted_page_records_the_hash_of_what_is_actually_on_disk(research_log):
    """That hash is what later refuses an update to a hand-edited page."""
    adopt(research_log)
    state = next(
        s for s in VaultIndex(research_log).load().all()
        if s.stem.startswith("Tether's")
    )
    assert state.content_sha == content_hash(LEGACY)


def test_adoption_is_idempotent(research_log):
    adopt(research_log)
    first = {p.name: p.read_text(encoding="utf-8")
             for p in (research_log / ".srb-state").glob("*.json")}
    adopt(research_log)
    second = {p.name: p.read_text(encoding="utf-8")
              for p in (research_log / ".srb-state").glob("*.json")}
    assert first == second


def test_two_pages_that_condense_to_one_key_still_get_separate_sidecars(research_log):
    (research_log / "Reserves audit.md").write_text(LEGACY, encoding="utf-8")
    (research_log / "reserves  audit.md").write_text(LEGACY, encoding="utf-8")
    adopt(research_log)
    index = VaultIndex(research_log).load()
    assert len(index.keys()) == 4, "a page was dropped by a key collision"
    assert len(index.stems()) == 4


def test_a_page_already_adopted_is_not_planned_again(research_log):
    adopt(research_log)
    assert plan(research_log, VaultIndex(research_log).load()) == []


def test_an_empty_directory_is_not_an_error(tmp_path):
    assert adopt(tmp_path / "absent") == 0
