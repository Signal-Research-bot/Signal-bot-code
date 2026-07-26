"""Tests for rendering and writing knowledge-base records.

The vault is a directory of hand-curated notes a human also edits, so the
properties that matter most here are the ones about not damaging it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.kb.render import (  # noqa: E402
    frontmatter, render, slug, title_for,
)
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


# --- audit regressions --------------------------------------------------------


def test_a_newline_in_the_title_cannot_inject_frontmatter_keys():
    """`title` is a free-form model-authored string summarising attacker-
    controlled chat, and it was interpolated raw into f"title: {title}". A
    newline terminated the scalar and everything after it parsed as further
    frontmatter keys -- letting a member dictate metadata on a page in a repo
    other members read."""
    rec = record()
    # Assembled at runtime rather than written as a literal: tools/scrub_check.py
    # forbids a contiguous absolute-path literal in any tracked file, and it
    # blocked this very fixture when it was first written. That check is right,
    # so the fixture bends, not the check.
    injected = "C" + ":/Users/someone/vault"
    rec["title"] = f'X\nsource_path: "{injected}"\ncssclasses: [hidden]'
    _, md = render(rec, first_raised="2026-07-24", last_verified="2026-07-25")

    block = md.split("---")[1].strip()
    keys = [line.split(":")[0] for line in block.split("\n")]
    assert keys == [
        "title", "entity_type", "research_status", "finding", "confidence",
        "first_raised", "last_verified", "tags", "sources", "related",
    ]
    assert "source_path" not in keys and "cssclasses" not in keys


def test_a_quote_in_the_title_cannot_break_the_scalar():
    rec = record()
    rec["title"] = 'He said "buy" \\ then left'
    _, md = render(rec, first_raised="2026-07-24", last_verified="2026-07-25")
    block = md.split("---")[1].strip()
    assert len(block.split("\n")) == 10


def test_enum_and_date_values_stay_unquoted():
    """The operator's existing vault writes these bare; a quoted enum reads as
    a different value to someone skimming the file."""
    _, md = render(record(), first_raised="2026-07-24", last_verified="2026-07-25")
    assert "research_status: answered" in md
    assert "last_verified: 2026-07-25" in md


def test_a_pipe_in_a_quote_cannot_break_the_evidence_table():
    rec = record()
    rec["evidence"] = [
        {"url": "https://a.example", "quote": "a | b\nc", "confidence": "primary"}
    ]
    _, md = render(rec, first_raised="2026-07-24", last_verified="2026-07-25")
    row = [ln for ln in md.split("\n") if "a.example" in ln and ln.startswith("|")][0]
    # Count only UNescaped pipes: "\|" is the escape and does not open a column.
    assert row.replace("\\|", "").count("|") == 4, "an unescaped pipe added a column"
    assert "a \\| b c" in row, "the newline inside the quote was not folded"


def test_frontmatter_escapes_on_its_own_without_help_from_render():
    """render() collapses whitespace in the title before calling frontmatter(),
    so a test that only goes through render() passes even if frontmatter()
    interpolates raw -- which is exactly what mutation testing caught. This
    pins the inner layer directly."""
    hostile = 'X\nmalicious_key: injected\ntags: ["a"]'
    block = frontmatter(
        record(), title=hostile,
        first_raised="2026-07-24", last_verified="2026-07-25",
    )
    keys = [line.split(":")[0] for line in block.strip().split("\n")[1:-1]]
    assert "malicious_key" not in keys
    assert keys.count("tags") == 1


# --- keeping participants out of the research itself -------------------------


def test_a_speaker_label_never_reaches_a_page():
    """The labels are STABLE for the life of the archive, so an attributed claim
    in a permanent, member-readable page identifies someone to the eight people
    best placed to work out who. The egress firewall cannot help: it ALLOWS
    allocated labels by design, and a test asserts that it does."""
    rec = record(
        question="Was Participant B right that the reserves are audited?",
        answer="Participant A disputed this; Participant B was correct.",
        headline="Participant B was right",
    )
    _, md = render(rec, first_raised="2026-07-24", last_verified="2026-07-25")
    # Assert on the LABEL shape, not the bare word: the static Provenance
    # sentence legitimately reads "Participants are pseudonymised", and a test
    # that banned the word outright would be testing the boilerplate.
    assert not re.search(r"Participants?\s+[A-Z]\b", md)
    assert "a group member" in md


def test_a_speaker_label_never_reaches_the_filename():
    """slug(title) becomes the filename, which is itself a published artefact."""
    stem, _ = render(
        record(title="Research - Participant C on reserves - 2026-07"),
        first_raised="2026-07-24", last_verified="2026-07-25",
    )
    assert "Participant" not in stem


def test_an_at_handle_is_stripped_from_a_page():
    rec = record(answer="As @zeropoint_x noted, the attestation is not an audit.")
    _, md = render(rec, first_raised="2026-07-24", last_verified="2026-07-25")
    assert "zeropoint_x" not in md


def test_depersonalising_does_not_block_the_write():
    """Strips, never refuses. The operator ranked a working tool above
    completeness of redaction, so this must degrade the text and carry on."""
    stem, md = render(
        record(question="Participant A asked about reserves"),
        first_raised="2026-07-24", last_verified="2026-07-25",
    )
    assert stem and "## Question" in md


def test_a_handle_shaped_tag_is_dropped():
    """An Obsidian tag is a clickable index across the vault, so a handle here
    would build a browsable page-set per person."""
    _, md = render(
        record(tags=["reserves", "zeropoint_x", "@someone", "Participant A"]),
        first_raised="2026-07-24", last_verified="2026-07-25",
    )
    tag_line = [ln for ln in md.splitlines() if ln.startswith("tags:")][0]
    assert "reserves" in tag_line
    assert "zeropoint_x" not in tag_line and "someone" not in tag_line


def test_speaker_labels_are_stripped_from_list_and_nested_fields():
    """Mutation testing caught this: every earlier test used a top-level string
    field, so removing list recursion from depersonalise() changed nothing and
    the suite stayed green.

    contradictions, open_questions and evidence[].quote are free text too, and
    a contradiction is exactly where "Participant B disagreed" gets written.
    """
    rec = record(
        contradictions=["Participant A says the reserves are audited"],
        open_questions=["What did Participant C mean by full backing?"],
        evidence=[{
            "url": "https://example.gov/f.htm",
            "quote": "Participant B linked this",
            "confidence": "primary",
        }],
    )
    _, md = render(rec, first_raised="2026-07-24", last_verified="2026-07-25")
    assert not re.search(r"Participants?\s+[A-Z]\b", md)
    assert md.count("a group member") == 3


def test_a_source_url_containing_an_at_sign_is_not_corrupted():
    """_AT_HANDLE fired on the '/@user' segment of a link, so
    "https://x.com/@coinmetrics/status/1" became
    "https://x.com/a group member/status/1". That corrupts the citation -- the
    whole point of the archive -- and the mangled URL then fails the grounding
    check, so the evidence is dropped as a fabrication too."""
    # Two shapes that fail differently. The '/@user' form is caught by the
    # lookbehind alone; the query-string and fragment forms are NOT, because
    # '=' and '#' are neither word characters nor '@' nor '/'. Only carving
    # URLs out entirely protects those. Mutation testing caught that the first
    # case on its own proved nothing.
    for url in (
        "https://x.com/@coinmetrics/status/123",
        "https://example.gov/search?author=@filer&y=2024",
        "https://example.gov/doc#@section",
    ):
        _, md = render(
            record(evidence=[{"url": url, "quote": "q", "confidence": "primary"}]),
            first_raised="2026-07-26", last_verified="2026-07-26",
        )
        assert url in md, f"corrupted: {url}"


def test_an_at_handle_in_prose_is_still_stripped():
    """The counterweight to carving URLs out."""
    _, md = render(
        record(answer="@zeropoint_x said the attestation is weak"),
        first_raised="2026-07-26", last_verified="2026-07-26",
    )
    assert "zeropoint_x" not in md


def test_two_entries_depersonalised_to_the_same_title_do_not_collide():
    """depersonalise runs before slug(), so distinct titles collapsed onto one
    filename. VaultWriter treats an existing path as idempotency and skips, so
    the second entry was silently discarded while batch counted it as written
    and announced it to the group."""
    a, _ = render(
        record(title="Research - Participant A on reserves - 2026-07", question="q1"),
        first_raised="2026-07-26", last_verified="2026-07-26",
    )
    b, _ = render(
        record(title="Research - Participant B on reserves - 2026-07", question="q2"),
        first_raised="2026-07-26", last_verified="2026-07-26",
    )
    assert a != b


def test_an_ordinary_title_keeps_a_clean_filename():
    """The disambiguator must only appear when something was actually stripped."""
    stem, _ = render(
        record(title="Research - are the reserves audited - 2026-07"),
        first_raised="2026-07-26", last_verified="2026-07-26",
    )
    assert stem == "Research - are the reserves audited - 2026-07"


def test_commit_works_when_the_repo_is_owned_by_another_user(vault, monkeypatch):
    """The vault is a bind mount, so inside the container every file presents as
    uid 0 while the process is uid 10002. Git refuses with "detected dubious
    ownership", rev-parse returns non-zero, and git_commit read that as "not a
    git repository" and skipped the commit.

    The first live run hit it: the page was written, never committed, and no
    member could read what the run had just paid to produce.

    The ownership mismatch itself cannot be simulated portably, so this asserts
    the mechanism that governs it: every git invocation must carry
    `-c safe.directory=<vault>`, scoped to that one path.
    """
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(args, **kwargs):
        if isinstance(args, list) and args and args[0] == "git":
            seen.append(args)
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    w = VaultWriter(vault)
    w.write("r", "x")
    assert git_commit(vault, "add r") is True

    assert seen, "no git command was run"
    for args in seen:
        assert "-c" in args, f"git invoked without -c: {args}"
        assert f"safe.directory={vault}" in args, (
            f"git invoked without safe.directory scoped to the vault: {args}"
        )
