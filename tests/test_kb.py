"""Tests for rendering and writing knowledge-base records.

The vault is a directory of hand-curated notes a human also edits, so the
properties that matter most here are the ones about not damaging it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.kb.render import render, slug, title_for  # noqa: E402
from signal_research_bot.kb.writer import VaultError, VaultWriter, git_commit  # noqa: E402


def record(**kw) -> dict:
    base = {
        "title": "Research - are the reserves audited - 2026-07",
        "question": "Are the reserves audited?",
        "answer": "The Q1 attestation is a review, not an audit.",
        "confidence": "primary",
        "research_status": "answered",
        "evidence": [
            {"url": "https://example.gov/f.htm", "quote": "agreed-upon procedures",
             "confidence": "primary"}
        ],
        "contradictions": [],
        "open_questions": [],
        "tags": ["reserves"],
    }
    base.update(kw)
    return base


# --- rendering ----------------------------------------------------------------


def test_frontmatter_uses_research_status_not_status():
    """`status` already carries a deal-lifecycle vocabulary in the operator's
    other vault; reusing it would poison every cross-vault query."""
    _, md = render(record(), first_raised="2026-07-24", last_verified="2026-07-25")
    assert "research_status: answered" in md
    assert "\nstatus:" not in md


def test_all_required_sections_present():
    _, md = render(record(), first_raised="2026-07-24", last_verified="2026-07-25")
    for heading in ("## Question", "## Answer", "## Evidence",
                    "## Contradictions", "## Open questions"):
        assert heading in md


def test_empty_contradictions_render_as_an_assertion_not_a_gap():
    """'None found' is a finding; a missing section is not."""
    _, md = render(record(), first_raised="2026-07-24", last_verified="2026-07-25")
    assert "_None found._" in md


def test_contested_record_gets_a_warning_callout():
    """Someone skimming must not read a disputed claim as settled."""
    _, md = render(
        record(research_status="contested", contradictions=["A says x, B says y"]),
        first_raised="2026-07-24", last_verified="2026-07-25",
    )
    assert "> [!warning] Contested" in md
    assert md.index("[!warning]") < md.index("## Answer")


def test_pipe_in_a_quote_does_not_break_the_table():
    _, md = render(
        record(evidence=[{"url": "https://e.example", "quote": "a | b",
                          "confidence": "primary"}]),
        first_raised="2026-07-24", last_verified="2026-07-25",
    )
    assert r"a \| b" in md


def test_newline_in_a_quote_does_not_break_the_table():
    _, md = render(
        record(evidence=[{"url": "https://e.example", "quote": "line one\nline two",
                          "confidence": "primary"}]),
        first_raised="2026-07-24", last_verified="2026-07-25",
    )
    table_line = [l for l in md.splitlines() if "line one" in l][0]
    assert "line two" in table_line


def test_no_evidence_says_so_explicitly():
    _, md = render(record(evidence=[]), first_raised="2026-07-24",
                   last_verified="2026-07-25")
    assert "_No sources were retrieved" in md


@pytest.mark.parametrize("bad", ['a/b', 'a\\b', 'a:b', 'a?b', 'a*b', 'a<b>c', 'a|b'])
def test_slug_strips_characters_windows_rejects(bad):
    assert not set(slug(bad)) & set('/\\:?*<>|')


def test_slug_never_returns_empty():
    assert slug("///") == "Untitled"


def test_title_is_truncated_but_stays_a_stable_link_target():
    t = title_for("x" * 300, "2026-07")
    assert len(t) < 120 and t.startswith("Research - ") and t.endswith("2026-07")


# --- writing ------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


def test_writes_into_its_own_subdirectory(vault):
    w = VaultWriter(vault)
    path = w.write("Research - a - 2026-07", "# a")
    assert path.parent.name == "Research Log"
    assert path.read_text(encoding="utf-8") == "# a"


def test_does_not_overwrite_an_existing_record(vault):
    """A re-run of a crashed batch must not clobber or duplicate."""
    w = VaultWriter(vault)
    w.write("r", "original")
    w.write("r", "replacement")
    assert (vault / "Research Log" / "r.md").read_text(encoding="utf-8") == "original"


def test_overwrite_is_possible_but_must_be_explicit(vault):
    w = VaultWriter(vault)
    w.write("r", "original")
    w.write("r", "replacement", overwrite=True)
    assert (vault / "Research Log" / "r.md").read_text(encoding="utf-8") == "replacement"


def test_traversal_in_a_title_cannot_escape_the_subdirectory(vault):
    w = VaultWriter(vault)
    with pytest.raises(VaultError):
        w.write("../../escaped", "x")


def test_leaves_no_temp_file_behind(vault):
    w = VaultWriter(vault)
    w.write("r", "x")
    assert list((vault / "Research Log").glob("*.tmp")) == []


def test_missing_vault_directory_is_refused(tmp_path):
    with pytest.raises(VaultError):
        VaultWriter(tmp_path / "absent")


def test_refuses_a_vault_inside_the_pre_existing_research_vault(tmp_path):
    """Guards against SRB_KB_DIR being pointed at the operator's other vault."""
    foreign = tmp_path / "CF"
    (foreign / "nested").mkdir(parents=True)
    with pytest.raises(VaultError):
        VaultWriter(foreign / "nested", foreign_vault_dir=foreign)


def test_digest_lists_titles_and_statuses_only(vault):
    w = VaultWriter(vault)
    w.write("Research - a - 2026-07", "---\nresearch_status: answered\n---\nbody text")
    digest = w.digest()
    assert "Research - a - 2026-07 [answered]" in digest
    assert "body text" not in digest, "digest must not carry answer bodies"


def test_digest_of_an_empty_archive(vault):
    assert "empty" in VaultWriter(vault).digest()


# --- git ----------------------------------------------------------------------


def test_commit_uses_a_neutral_identity(vault):
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    w = VaultWriter(vault)
    w.write("r", "x")
    assert git_commit(vault, "add r") is True

    out = subprocess.run(
        ["git", "-C", str(vault), "log", "--format=%an <%ae>", "-1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "signal-research-bot <signal-research-bot@users.noreply.github.com>"


def test_commit_is_a_no_op_when_nothing_changed(vault):
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    VaultWriter(vault).write("r", "x")
    git_commit(vault, "first")
    assert git_commit(vault, "second") is False


def test_non_git_vault_does_not_raise(vault):
    VaultWriter(vault).write("r", "x")
    assert git_commit(vault, "m") is False
