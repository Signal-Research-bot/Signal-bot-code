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
from signal_research_bot.kb.writer import (  # noqa: E402
    VaultError, VaultWriter, WriteOutcome, git_commit,
)


# Exactly these keys, in this order, and nothing else. Two tests read from this:
# one asserts the list (the injection guarantee -- a title must not be able to
# add a key), the other asserts the block's line count. Kept exact rather than a
# subset check, and in one place so a schema change updates one literal.
EXPECTED_FRONTMATTER_KEYS = [
    "title", "entity_type", "topic_key", "research_status", "finding",
    "confidence", "first_raised", "last_verified", "tags", "sources", "related",
]


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
    result = w.write("Research - a - 2026-07", "# a")
    assert result.path.parent.name == "Research Log"
    assert result.path.read_text(encoding="utf-8") == "# a"


# --- what a write reports back ------------------------------------------------
#
# The writer used to return a Path whether it wrote or skipped, so the caller
# could not tell the difference and counted both as written. A discarded page
# was committed and announced to the group as a new entry.


def test_write_reports_that_it_created_the_file(vault):
    result = VaultWriter(vault).write("r", "body")
    assert result.outcome is WriteOutcome.CREATED
    assert result.wrote is True


def test_a_second_distinct_record_is_reported_as_a_collision_not_a_write(vault):
    """The regression. Two different findings, one filename: the second is not
    written, and the caller must be able to see that it was not."""
    w = VaultWriter(vault)
    w.write("r", "the first finding")
    result = w.write("r", "a completely different finding")
    assert result.outcome is WriteOutcome.COLLIDED
    assert result.wrote is False
    assert result.path.read_text(encoding="utf-8") == "the first finding"


def test_an_identical_rewrite_is_reported_as_unchanged_not_a_collision(vault):
    """A crashed batch re-running is not data loss, and must not be reported as
    though it were -- or every benign re-run cries wolf."""
    w = VaultWriter(vault)
    w.write("r", "same bytes")
    result = w.write("r", "same bytes")
    assert result.outcome is WriteOutcome.UNCHANGED
    assert result.wrote is False


def test_explicit_overwrite_reports_replaced(vault):
    w = VaultWriter(vault)
    w.write("r", "original")
    result = w.write("r", "replacement", overwrite=True)
    assert result.outcome is WriteOutcome.REPLACED
    assert result.wrote is True


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


# --- fitting the vault's house style ------------------------------------------


def _fm(confidence="primary", **kw):
    from signal_research_bot.kb.render import frontmatter

    record = {"research_status": "answered", "confidence": confidence,
              "evidence": [], "tags": []}
    record.update(kw)
    return frontmatter(record, title="T", first_raised="2026-07-24",
                       last_verified="2026-07-26")


def test_a_positive_evidence_grade_is_written_to_the_confidence_key():
    assert "confidence: primary" in _fm("primary")
    assert "confidence: corroborated" in _fm("corroborated")


def test_a_weak_grade_becomes_a_tag_and_the_confidence_key_is_omitted():
    """The vault's `confidence:` key is an evidence grade that has only ever
    held two values across 88 hand-written pages; the weaker grades live as
    tags, on pages carrying no `confidence:` at all. Writing four values into
    that key makes the field ambiguous corpus-wide with no way to tell
    afterwards which scale a page used -- and the vault's Dashboard has a
    Confidence Breakdown table reading exactly it."""
    block = _fm("single-source")
    assert "confidence:" not in block
    assert '"single-source"' in block

    block = _fm("unverified")
    assert "confidence:" not in block
    assert '"unverified"' in block


def test_the_real_grade_is_never_lost_even_when_it_leaves_the_page():
    """It stays in the sidecar, which is what ranks the stronger value on an
    update. The divergence is at the render boundary only."""
    from signal_research_bot.kb.state import state_from_record

    state = state_from_record(
        {"confidence": "single-source", "question": "q"},
        topic_key="k", stem="s", title="t", today="2026-07-26",
    )
    assert state.confidence == "single-source"


def test_the_bot_can_never_write_the_vaults_status_key():
    """`status:` holds a deal-lifecycle vocabulary on 88 pages -- completed,
    closed, announced, and free text. Research state in that key corrupts every
    page and every table that reads it."""
    block = _fm(research_status="open")
    assert "\nstatus:" not in block
    assert "research_status: open" in block


def test_near_miss_tags_are_folded_onto_the_vaults_own_spelling():
    """Two spellings of one idea produce two tags, two filtered views, and a
    reader who finds half the pages."""
    block = _fm(tags=["stablecoins", "bitcoin-mining", "tether"])
    assert '"stablecoin"' in block and '"stablecoins"' not in block
    assert '"mining"' in block and '"bitcoin-mining"' not in block
    assert '"tether"' in block


def test_a_sidecar_never_stores_an_unstripped_speaker_label():
    """Sidecars live inside the vault and are committed with it. render() strips
    a copy of the record on its way to the page; nothing stripped the record on
    its way here, so a stable per-person label attached to a claim was reaching
    a member-readable repo by the one path that never called depersonalise."""
    from signal_research_bot.kb.state import state_from_record

    state = state_from_record(
        {"question": "Was Participant B right?",
         "answer": "Participant C disagreed.",
         "headline": "Participant A was wrong."},
        topic_key="k", stem="s", title="t", today="2026-07-26",
    )
    blob = f"{state.question} {state.answer} {state.headline}"
    assert "Participant" not in blob
    assert blob.count("a group member") == 3


def test_a_page_is_named_for_the_vaults_type_subject_grammar():
    """Every hand-written page in the vault is "Type - Subject[ - Date]". A page
    that is not reads as an intruder, and sorts away from its own kind."""
    from signal_research_bot.kb.render import research_title

    assert research_title("Tether's reserve attestations") == (
        "Research - Tether's reserve attestations"
    )


def test_the_prefix_is_not_applied_twice():
    from signal_research_bot.kb.render import research_title

    assert research_title("Research - already prefixed") == "Research - already prefixed"


def test_a_model_title_cannot_aim_at_another_page_type(tmp_path):
    """A record titled "Company - Anchorage Digital" would otherwise target the
    filename of a page a human wrote. The writer refuses and reports a
    collision, which is safe -- but it is research lost to a naming accident."""
    from signal_research_bot.kb.render import render

    stem, _ = render(
        {"title": "Company - Anchorage Digital", "question": "q", "answer": "a",
         "research_status": "answered", "confidence": "primary", "evidence": []},
        first_raised="2026-07-26", last_verified="2026-07-26",
    )
    assert stem == "Research - Company - Anchorage Digital"


def test_a_slash_in_a_title_does_not_weld_two_words_together():
    """Deleting the separator produced "TreasuryFed", "BlackRockAnchorage" and
    "ZaguryElektronPeak" on three of the five live pages. The filename is the
    permanent wikilink target, so a mangled one is mangled forever."""
    assert slug("Treasury/Fed under Yellen") == "Treasury-Fed under Yellen"
    assert slug("Zagury/Elektron/Peak mining") == "Zagury-Elektron-Peak mining"


def test_a_page_links_back_to_the_vaults_hub_pages():
    """The convention 271 of 272 hand-written pages follow, and the reason the
    vault has two orphans. A generated page that skips it is an orphan by
    construction."""
    from signal_research_bot.kb.render import render

    _, markdown = render(
        {"title": "T", "question": "q", "answer": "a", "research_status": "open",
         "confidence": "unverified", "evidence": []},
        first_raised="2026-07-26", last_verified="2026-07-26",
        hubs=("Investment Network Overview", "Timeline"),
    )
    assert "## Related Pages" in markdown
    assert "- [[Investment Network Overview]]" in markdown
    assert "- [[Timeline]]" in markdown
    # The same links are in frontmatter, and two lists that disagree is worse
    # than either.
    assert 'related: ["[[Investment Network Overview]]", "[[Timeline]]"]' in markdown


def test_a_hub_already_in_related_is_not_listed_twice():
    from signal_research_bot.kb.render import render

    _, markdown = render(
        {"title": "T", "question": "q", "answer": "a", "research_status": "open",
         "confidence": "unverified", "evidence": []},
        first_raised="2026-07-26", last_verified="2026-07-26",
        related=["[[Timeline]]"], hubs=("Timeline",),
    )
    assert markdown.count("[[Timeline]]") == 2, "once in frontmatter, once in the body"


def test_no_hubs_configured_means_no_backlink_block():
    """The hub page names belong to one operator's vault; this module does not."""
    from signal_research_bot.kb.render import render

    _, markdown = render(
        {"title": "T", "question": "q", "answer": "a", "research_status": "open",
         "confidence": "unverified", "evidence": []},
        first_raised="2026-07-26", last_verified="2026-07-26",
    )
    assert "## Related Pages" not in markdown


def test_digest_lists_titles_and_statuses_only(vault):
    w = VaultWriter(vault)
    w.write(
        "Research - a - 2026-07",
        "---\nresearch_status: answered\ntopic_key: a-topic\n---\nbody text",
    )
    digest = w.digest()
    assert "Research - a - 2026-07 [answered] (key: a-topic)" in digest
    assert "body text" not in digest, "digest must not carry answer bodies"


def test_digest_indexes_the_whole_vault_not_one_directory(vault):
    """The central defect this fixes: triage was shown five pages out of 277 and
    researched five subjects the vault already covered, from primary sources, at
    Opus prices. Deduplication against an archive you cannot see is not
    deduplication."""
    (vault / "Companies").mkdir()
    (vault / "Companies" / "Company - Anchorage Digital.md").write_text(
        "---\ntitle: Company - Anchorage Digital\nentity_type: company\n---\n",
        encoding="utf-8",
    )
    VaultWriter(vault).write("Research - a - 2026-07", "---\nresearch_status: open\n---\n")

    digest = VaultWriter(vault).digest()
    assert "Company - Anchorage Digital [company]" in digest
    assert "Research - a - 2026-07" in digest
    assert "Companies/" in digest, "the folder taxonomy is part of the signal"


def test_a_hand_written_page_is_listed_without_a_key(vault):
    """The difference triage acts on: a key means "you may update this page",
    no key means "this subject is covered, you may only call it a duplicate"."""
    (vault / "Companies").mkdir()
    (vault / "Companies" / "Company - X.md").write_text(
        "---\nentity_type: company\n---\n", encoding="utf-8"
    )
    assert "(key:" not in VaultWriter(vault).digest()


def test_the_bots_own_bookkeeping_is_not_offered_as_an_archive_entry(vault):
    """Otherwise triage dedupes real research against the changelog, or against
    the index page that lists the research."""
    for sub in ("Changelog", "Dashboard"):
        (vault / sub).mkdir()
        (vault / sub / "thing.md").write_text("---\ntitle: t\n---\n", encoding="utf-8")
    assert "thing" not in VaultWriter(vault).digest()


def test_a_truncated_index_says_so(vault):
    """A truncated listing reads exactly like a complete one, and the
    consequence is the bot re-researching whatever fell off the end."""
    (vault / "Companies").mkdir()
    for i in range(5):
        (vault / "Companies" / f"Company - {i}.md").write_text(
            "---\nentity_type: company\n---\n", encoding="utf-8"
        )
    digest = VaultWriter(vault).digest(limit=3)
    assert "2 further page(s) not listed" in digest


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
    assert keys == EXPECTED_FRONTMATTER_KEYS
    assert "source_path" not in keys and "cssclasses" not in keys


def test_a_quote_in_the_title_cannot_break_the_scalar():
    rec = record()
    rec["title"] = 'He said "buy" \\ then left'
    _, md = render(rec, first_raised="2026-07-24", last_verified="2026-07-25")
    block = md.split("---")[1].strip()
    assert len(block.split("\n")) == len(EXPECTED_FRONTMATTER_KEYS)


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
