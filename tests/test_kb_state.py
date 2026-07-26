"""Tests for topic keys, the topic index, and how an update folds into a page.

A page is a living document now. The properties that matter are the ones about
not losing what is already on it, and not moving it.
"""

from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.kb.render import (  # noqa: E402
    bump_last_verified, normalise_topic_key, render, render_update_block, slug,
)
from signal_research_bot.kb.state import (  # noqa: E402
    TopicState, VaultIndex, merge, state_from_record,
)


def record(**kw) -> dict:
    base = {
        "title": "Are the reserves audited",
        "topic_key": "reserves-audit",
        "question": "Are the reserves audited?",
        "answer": "The Q1 attestation is a review, not an audit.",
        "headline": "The Q1 report is a review, not an audit.",
        "confidence": "corroborated",
        "research_status": "answered",
        "finding": "mixed",
        "evidence": [{"url": "https://a.example/1", "quote": "q1",
                      "confidence": "corroborated"}],
        "contradictions": ["a disagrees with b"],
        "open_questions": ["who signed it"],
        "tags": ["reserves", "attestation"],
    }
    base.update(kw)
    return base


def state(**kw) -> TopicState:
    base = state_from_record(
        record(), topic_key="reserves-audit", stem="Are the reserves audited",
        title="Are the reserves audited", today="2026-07-26",
    )
    return replace(base, **kw)


# --- topic keys ---------------------------------------------------------------


def test_a_topic_key_cannot_traverse_out_of_the_subdirectory():
    """The output alphabet has no separator in it, so this holds by construction
    rather than by a check someone can forget to call."""
    key = normalise_topic_key("../../etc/passwd")
    assert key is not None
    assert not set(key) & set('./\\:')


@pytest.mark.parametrize("reserved", ["nul", "CON", "com1", "LPT9", "prn"])
def test_a_windows_device_name_is_never_a_topic_key(reserved):
    """"nul.md" is not a writable file on Windows, whatever the extension."""
    assert normalise_topic_key(reserved) is None


def test_a_topic_key_carrying_a_speaker_label_is_stripped():
    """The key is permanent and member-visible, and depersonalise only matches
    the prose form -- 'participant-b' would sail straight past it."""
    key = normalise_topic_key("participant-b-on-reserves")
    assert key is not None
    assert "participant" not in key and re.search(r"\bb\b", key) is None


def test_a_topic_key_from_a_prose_speaker_label_is_stripped():
    key = normalise_topic_key("Participant B on reserves")
    assert key is not None and "participant" not in key


def test_a_topic_key_carrying_an_at_handle_is_stripped():
    """The slug pattern only knows the 'participant-x' shape. A chat handle is
    an ordinary word to it, and would survive into a filename and the digest --
    depersonalise is what catches it."""
    key = normalise_topic_key("@zeropoint_x on reserves")
    assert key is not None
    assert "zeropoint" not in key


@pytest.mark.parametrize("junk", ["", "   ", "2026", "---", "!!!"])
def test_an_unusable_topic_key_is_rejected_rather_than_mangled(junk):
    assert normalise_topic_key(junk) is None


def test_a_topic_key_is_emitted_bare_so_it_stays_greppable():
    """The index can be rebuilt from the vault with a prefix scan only while the
    value is unquoted."""
    rec = record(topic_key="reserves-audit")
    _, md = render(rec, first_raised="2026-07-26", last_verified="2026-07-26",
                   topic_key="reserves-audit")
    assert "topic_key: reserves-audit" in md


def test_a_long_topic_key_is_truncated_at_a_word_boundary():
    key = normalise_topic_key("a" + "-word" * 40)
    assert key is not None and len(key) <= 64 and not key.endswith("-")


# --- the filename never moves -------------------------------------------------


def test_an_update_keeps_the_original_filename_even_when_the_title_changes():
    """The filename is the wikilink target; renaming breaks every inbound link."""
    stem, _ = render(
        record(title="A completely different phrasing"),
        first_raised="2026-07-26", last_verified="2026-08-02",
        topic_key="reserves-audit", stem="Are the reserves audited",
    )
    assert stem == "Are the reserves audited"


def test_slug_strips_characters_that_break_a_wikilink():
    for ch in "#[]^":
        assert ch not in slug(f"reserves {ch} audit")


def test_two_topics_with_the_same_title_get_distinct_filenames(tmp_path):
    index = VaultIndex(tmp_path)
    index.put(state())
    assert index.reserve_stem("Are the reserves audited") == "Are the reserves audited (2)"


# --- what an update does to a page --------------------------------------------


def test_first_raised_never_advances():
    merged, _ = merge(state(), record(), today="2026-09-01")
    assert merged.first_raised == "2026-07-26"


def test_last_verified_advances():
    merged, _ = merge(state(), record(), today="2026-09-01")
    assert merged.last_verified == "2026-09-01"


def test_evidence_is_unioned_not_replaced():
    """The evidence table is the archive's spine. A row dropped because a later
    search did not re-find it is evidence destroyed."""
    merged, _ = merge(
        state(),
        record(evidence=[{"url": "https://b.example/2", "quote": "q2",
                          "confidence": "primary"}]),
        today="2026-09-01",
    )
    urls = [e["url"] for e in merged.evidence]
    assert urls == ["https://a.example/1", "https://b.example/2"]


def test_open_questions_are_unioned_not_replaced():
    merged, _ = merge(state(), record(open_questions=["and who paid"]), today="2026-09-01")
    assert merged.open_questions == ("who signed it", "and who paid")


def test_contradictions_are_unioned_not_replaced():
    merged, _ = merge(state(), record(contradictions=["c disagrees too"]), today="2026-09-01")
    assert merged.contradictions == ("a disagrees with b", "c disagrees too")


def test_tags_are_unioned_so_a_page_is_never_orphaned_from_its_topic():
    merged, _ = merge(state(), record(tags=["audit"]), today="2026-09-01")
    assert set(merged.tags) == {"reserves", "attestation", "audit"}


def test_a_contested_status_is_sticky():
    """A disputed claim must not stop being flagged because one re-check
    came back clean."""
    merged, _ = merge(
        state(research_status="contested"), record(research_status="answered"),
        today="2026-09-01",
    )
    assert merged.research_status == "contested"


def test_confidence_never_downgrades_on_a_thinner_update():
    """Evidence already gathered does not weaken. A genuine conflict surfaces as
    contested, not as a quiet downgrade."""
    merged, _ = merge(
        state(confidence="primary"), record(confidence="single-source"),
        today="2026-09-01",
    )
    assert merged.confidence == "primary"


def test_a_changed_finding_is_stated_verbatim_rather_than_averaged():
    """A reversal is the most important thing an update can carry."""
    _, entry = merge(state(finding="mixed"), record(finding="refuted"), today="2026-09-01")
    assert any("mixed → refuted" in c for c in entry["changes"])


def test_an_update_records_only_the_sources_that_are_new():
    _, entry = merge(
        state(),
        record(evidence=[
            {"url": "https://a.example/1", "quote": "q1", "confidence": "corroborated"},
            {"url": "https://c.example/3", "quote": "q3", "confidence": "primary"},
        ]),
        today="2026-09-01",
    )
    assert entry["sources"] == ["https://c.example/3"]


def test_updates_accumulate_oldest_first():
    once, _ = merge(state(), record(), today="2026-08-01")
    twice, _ = merge(once, record(), today="2026-09-01")
    assert [u["date"] for u in twice.updates] == ["2026-08-01", "2026-09-01"]


def test_the_updates_section_reaches_the_page():
    merged, _ = merge(state(), record(finding="supported"), today="2026-09-01")
    _, md = render(
        merged.as_record(), first_raised=merged.first_raised,
        last_verified=merged.last_verified, topic_key=merged.topic_key,
        stem=merged.stem, updates=list(merged.updates),
    )
    assert "## Updates" in md and "### 2026-09-01" in md
    assert "mixed → supported" in md


# --- pages the bot cannot re-render ------------------------------------------


def test_bump_last_verified_changes_exactly_one_line():
    page = "---\ntitle: x\nlast_verified: 2026-07-26\n---\n\nbody\n"
    out = bump_last_verified(page, "2026-09-01")
    assert out is not None
    assert out.count("2026-09-01") == 1 and "2026-07-26" not in out
    assert out.endswith("\nbody\n")


@pytest.mark.parametrize("page", [
    '---\nlast_verified: "2026-07-26"\n---\n',      # quoted: not the shape we write
    "---\ntitle: x\n---\n",                          # key absent
    "no frontmatter at all\nlast_verified: 2026-07-26\n",
    "---\nlast_verified: 2026-07-26\nlast_verified: 2026-07-27\n---\n",
])
def test_bump_last_verified_refuses_anything_it_does_not_recognise(page):
    """A lenient reader of hand-edited YAML writes a wrong value into a
    permanent page. This one would rather do nothing and say so."""
    assert bump_last_verified(page, "2026-09-01") is None


# --- the index ----------------------------------------------------------------


def test_a_topic_survives_a_round_trip_through_disk(tmp_path):
    index = VaultIndex(tmp_path)
    original = state(updates=({"date": "2026-08-01", "headline": "h",
                               "changes": [], "sources": []},))
    index.put(original)
    reloaded = VaultIndex(tmp_path).load().get("reserves-audit")
    assert reloaded == original


def test_state_lives_in_a_dot_directory_obsidian_ignores(tmp_path):
    index = VaultIndex(tmp_path)
    index.put(state())
    assert (tmp_path / ".srb-state" / "reserves-audit.json").exists()
    assert list(tmp_path.glob("*.json")) == []


def test_an_unreadable_sidecar_does_not_take_the_archive_down(tmp_path):
    index = VaultIndex(tmp_path)
    index.put(state())
    (tmp_path / ".srb-state" / "broken.json").write_text("{not json", encoding="utf-8")
    assert VaultIndex(tmp_path).load().keys() == {"reserves-audit"}


def test_the_index_leaves_no_temp_file_behind(tmp_path):
    index = VaultIndex(tmp_path)
    index.put(state())
    assert list((tmp_path / ".srb-state").glob("*.tmp")) == []


# --- related links ------------------------------------------------------------


def test_related_links_topics_that_share_topical_tags(tmp_path):
    index = VaultIndex(tmp_path)
    index.put(state(topic_key="other", stem="Other page",
                    tags=("reserves", "attestation", "audit")))
    assert index.related_stems("reserves-audit", ("reserves", "attestation")) == \
        ["[[Other page]]"]


def test_related_ignores_the_tags_every_page_carries(tmp_path):
    """render() puts signal-derived and research on every page. Counting them,
    every page would link to every page."""
    index = VaultIndex(tmp_path)
    index.put(state(topic_key="other", stem="Other page",
                    tags=("signal-derived", "research", "mining")))
    assert index.related_stems("reserves-audit", ("signal-derived", "research")) == []


def test_a_page_never_links_to_itself(tmp_path):
    index = VaultIndex(tmp_path)
    index.put(state())
    assert index.related_stems("reserves-audit", ("reserves", "attestation")) == []


def test_related_is_deterministic_so_a_rerun_produces_the_same_page(tmp_path):
    index = VaultIndex(tmp_path)
    for n in range(4):
        index.put(state(topic_key=f"t{n}", stem=f"Page {n}",
                        tags=("reserves", "attestation")))
    first = index.related_stems("reserves-audit", ("reserves", "attestation"))
    assert first == VaultIndex(tmp_path).load().related_stems(
        "reserves-audit", ("reserves", "attestation")
    )


# --- participants stay out of updates -----------------------------------------


def test_an_update_block_strips_speaker_labels():
    """render_update_block is the only function that formats an update, and it
    depersonalises internally -- so the legacy append path, which does not go
    through render(), cannot bypass the strip."""
    block = render_update_block({
        "date": "2026-09-01",
        "headline": "Participant B was right about the reserves.",
        "changes": ["Participant A disputed it."],
        "sources": ["https://a.example/@handle/1"],
    })
    assert not re.search(r"Participants?\s+[A-Z]\b", block)
    assert "a group member" in block
    assert "https://a.example/@handle/1" in block, "a source URL must not be rewritten"
