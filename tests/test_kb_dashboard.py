"""Tests for the generated index page.

Two properties carry this module. It must be a *complete* index -- nothing in
the archive may be unreachable from it -- and it must be *byte-stable*, because
`_finish` commits when anything was written and a dashboard that churns turns
the vault history into noise.

Every fixture is synthetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.kb.dashboard import (  # noqa: E402
    DASHBOARD_STEM,
    DASHBOARD_SUBDIR,
    render_dashboard,
    write_dashboard,
)
from signal_research_bot.kb.state import TopicState, VaultIndex  # noqa: E402
from signal_research_bot.kb.writer import VaultWriter  # noqa: E402


def state(key: str, stem: str, **kw) -> TopicState:
    base = dict(
        topic_key=key, stem=stem, title=stem,
        research_status="answered", confidence="corroborated",
        finding="supported", last_verified="2026-07-26",
        tags=("research", "signal-derived", "tether"),
    )
    base.update(kw)
    return TopicState(**base)


@pytest.fixture
def index(tmp_path):
    idx = VaultIndex(tmp_path / "Research Log")
    idx.stage(state("stake", "Tether's equity stake"))
    idx.stage(state("reserves", "Proof of reserves", research_status="contested",
                    confidence="single-source", finding="mixed",
                    tags=("research", "reserves", "signal-derived", "tether")))
    idx.stage(state("quantum", "Quantum security claims", research_status="open",
                    confidence="unverified", finding="unestablished",
                    last_verified="", tags=("quantum-security", "research")))
    return idx


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Research Log").mkdir(parents=True)
    return VaultWriter(tmp_path)


# --- completeness -------------------------------------------------------------


def test_every_topic_is_reachable_from_the_dashboard(index):
    """The whole point. A page missing from the index is a page nobody finds."""
    page = render_dashboard(index)
    for s in index.all():
        assert f"[[{s.stem}]]" in page


def test_topics_are_grouped_by_status_with_contested_first(index):
    """Reading order, not the precedence order state.py sorts by -- which puts
    `dropped` first and would open the archive's index with its dead entries."""
    page = render_dashboard(index)
    assert page.index("## Contested") < page.index("## Open") < page.index("## Answered")


def test_a_status_the_display_order_does_not_know_still_gets_a_section(index):
    """A sixth value added to the schema must not silently vanish from the
    index, which is exactly what a fixed list of headings would do."""
    index.stage(state("odd", "Something else", research_status="withdrawn"))
    page = render_dashboard(index)
    assert "Withdrawn" in page and "[[Something else]]" in page


def test_an_entry_carries_the_finding_confidence_and_date(index):
    assert (
        "- [[Proof of reserves]] — mixed, single-source, last verified 2026-07-26"
        in render_dashboard(index)
    )


def test_a_topic_never_verified_says_so_rather_than_showing_a_blank(index):
    assert "[[Quantum security claims]] — unestablished, unverified, last verified not recorded" \
        in render_dashboard(index)


def test_the_tag_index_lists_tags_that_connect_two_or_more_topics(index):
    page = render_dashboard(index)
    assert "- **tether** — [[Proof of reserves]], [[Tether's equity stake]]" in page


def test_tags_on_a_single_page_are_counted_rather_than_dropped_silently(index):
    """A cap that is not stated reads as completeness."""
    assert "further tag(s) appear on a single page each" in render_dashboard(index)


def test_the_counts_table_totals_every_topic(index):
    assert "| **total** | **3** |" in render_dashboard(index)


def test_an_empty_archive_renders_rather_than_crashing(tmp_path):
    page = render_dashboard(VaultIndex(tmp_path / "Research Log"))
    assert "_The archive is empty._" in page
    assert "| **total** | **0** |" in page


# --- byte stability -----------------------------------------------------------


def test_the_same_index_renders_the_same_bytes(index):
    """No clock, no set iteration order, no dict ordering leaking through."""
    assert render_dashboard(index) == render_dashboard(index)


def test_no_date_that_is_not_a_topic_s_own_last_verified(index):
    """A generated-on line would change the file every run, so the batch would
    commit and push a revision of it on every window."""
    page = render_dashboard(index)
    assert page.count("2026-07-26") == 2, "one per topic that has a date, no more"


def test_a_second_run_over_an_unchanged_archive_writes_nothing(vault, index):
    assert write_dashboard(vault, index) is True
    path = vault.vault_dir / DASHBOARD_SUBDIR / f"{DASHBOARD_STEM}.md"
    before = path.stat().st_mtime_ns
    assert write_dashboard(vault, index) is False
    assert path.stat().st_mtime_ns == before, "the file was rewritten anyway"


def test_a_changed_archive_does_rewrite_it(vault, index):
    write_dashboard(vault, index)
    index.stage(state("new", "A new topic"))
    assert write_dashboard(vault, index) is True
    page = (vault.vault_dir / DASHBOARD_SUBDIR / f"{DASHBOARD_STEM}.md").read_text(
        encoding="utf-8"
    )
    assert "[[A new topic]]" in page


# --- staying out of the way ---------------------------------------------------


def test_the_dashboard_is_never_offered_to_triage(vault, index):
    """It lives outside Research Log/ so digest() cannot see it. Otherwise
    triage reads the archive's own index page as an archive entry and dedupes
    real research against it."""
    write_dashboard(vault, index)
    assert DASHBOARD_STEM not in vault.digest()


def test_the_dashboard_carries_a_do_not_edit_header(vault, index):
    write_dashboard(vault, index)
    page = (vault.vault_dir / DASHBOARD_SUBDIR / f"{DASHBOARD_STEM}.md").read_text(
        encoding="utf-8"
    )
    assert "edit a topic's own page, not this one" in page


def test_a_speaker_label_never_survives_into_the_index_page(tmp_path):
    """Stems on adopted pages are filenames from before the index existed and
    were never depersonalised by anything."""
    idx = VaultIndex(tmp_path / "Research Log")
    idx.stage(state("legacy", "Participant B on reserves"))
    assert "Participant B" not in render_dashboard(idx)
