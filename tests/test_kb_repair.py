"""Tests for repairing pages adopted before the topic index knew anything.

Adoption deliberately read nothing out of a page. Repair reads its frontmatter
and writes exactly two lines back, so these tests are about blast radius: what
it recovers, and -- more importantly -- what it refuses to touch and how little
it changes when it does act.

Every fixture is synthetic. `.claude/skills/privacy-invariants` forbids test
data captured from a real run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.kb.adopt import adopt  # noqa: E402
from signal_research_bot.kb.repair import patch, repair  # noqa: E402
from signal_research_bot.kb.state import VaultIndex, content_hash  # noqa: E402
from signal_research_bot.kb.writer import VaultWriter  # noqa: E402

# Two pages sharing two substantive tags, so `related` is non-empty, and one
# that shares none, so an isolated page stays isolated rather than being linked
# to everything.
STAKE = "Tether's equity stake in Anchorage Digital"
RESERVES = "Has Tether provided verifiable proof of reserves"
QUANTUM = "Quantum security claims"


def page(
    title: str,
    tags: list[str],
    *,
    status: str = "answered",
    finding: str = "supported",
    confidence: str = "corroborated",
) -> str:
    """A page in the legacy shape: no topic_key, related empty."""
    quoted = "[" + ", ".join(f'"{t}"' for t in tags) + "]"
    return (
        "---\n"
        f'title: "{title}"\n'
        "entity_type: research_task\n"
        f"research_status: {status}\n"
        f"finding: {finding}\n"
        f"confidence: {confidence}\n"
        "first_raised: 2026-07-24\n"
        "last_verified: 2026-07-26\n"
        f"tags: {quoted}\n"
        'sources: ["https://a.example/1"]\n'
        "related: []\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        "**A finding.**\n"
        "\n"
        "## Provenance\n"
        "\n"
        "Raised in the group chat and researched automatically.\n"
    )


@pytest.fixture
def vault(tmp_path):
    d = tmp_path / "Research Log"
    d.mkdir(parents=True)
    (d / f"{STAKE}.md").write_text(
        page(STAKE, ["research", "signal-derived", "stablecoins", "tether"]),
        encoding="utf-8",
    )
    (d / f"{RESERVES}.md").write_text(
        page(RESERVES, ["research", "reserves", "signal-derived", "stablecoins", "tether"],
             status="contested", finding="mixed", confidence="single-source"),
        encoding="utf-8",
    )
    (d / f"{QUANTUM}.md").write_text(
        page(QUANTUM, ["quantum-security", "research", "signal-derived"],
             status="open", finding="unestablished", confidence="unverified"),
        encoding="utf-8",
    )
    adopt(d)
    return d


def _state(vault_dir: Path, stem: str):
    return next(s for s in VaultIndex(vault_dir).load().all() if s.stem == stem)


def _fm(text: str) -> list[str]:
    return text.split("\n")[1:text.split("\n").index("---", 1)]


# --- doing nothing ------------------------------------------------------------


def test_a_dry_run_writes_absolutely_nothing(vault):
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    repair(vault, dry_run=True)
    assert {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()} == before


def test_a_page_with_no_topic_state_is_skipped(vault):
    """Repair is not a substitute for adoption, and must not invent a key."""
    (vault / "Never adopted.md").write_text(page("Never adopted", ["research"]),
                                            encoding="utf-8")
    before = (vault / "Never adopted.md").read_text(encoding="utf-8")
    assert repair(vault) == 1
    assert (vault / "Never adopted.md").read_text(encoding="utf-8") == before


def test_a_hand_edited_page_is_refused(vault):
    """kb-schema's "never edits a human-authored page" is not relaxed here."""
    path = vault / f"{STAKE}.md"
    edited = path.read_text(encoding="utf-8") + "\nA human wrote this.\n"
    path.write_text(edited, encoding="utf-8")
    assert repair(vault) == 1
    assert path.read_text(encoding="utf-8") == edited


def test_a_tags_line_we_did_not_write_is_refused(vault):
    """Not a YAML parser, by design. `related` is computed FROM these tags, so
    reading a shape we do not recognise means writing a wrong list into a
    permanent page."""
    path = vault / f"{QUANTUM}.md"
    text = path.read_text(encoding="utf-8").replace(
        'tags: ["quantum-security", "research", "signal-derived"]',
        "tags: [quantum-security, research]",
    )
    path.write_text(text, encoding="utf-8")
    _resync(vault, path)              # so only the SHAPE is at issue here
    assert repair(vault) == 1
    assert path.read_text(encoding="utf-8") == text


def test_a_topic_key_belonging_to_something_else_is_never_reassigned():
    """Repointing a key silently re-targets every future update at this page."""
    text = page("X", ["research"]).replace(
        "entity_type: research_task",
        "entity_type: research_task\ntopic_key: someone-elses-key",
    )
    assert patch(text, topic_key="ours", related=[]) is None


@pytest.mark.parametrize(
    "mangle",
    [
        lambda t: t.replace("entity_type: research_task\n", ""),      # no anchor
        lambda t: t.replace("related: []\n", ""),                     # no related
        lambda t: t.replace("---\n", "", 1),                          # no fences
    ],
)
def test_a_page_missing_an_anchor_is_left_alone(mangle):
    assert patch(mangle(page("X", ["research"])), topic_key="x", related=[]) is None


# --- what it recovers ---------------------------------------------------------


def test_the_topic_key_is_inserted_directly_after_entity_type(vault):
    repair(vault)
    lines = _fm((vault / f"{STAKE}.md").read_text(encoding="utf-8"))
    assert lines[lines.index("entity_type: research_task") + 1] == (
        "topic_key: tether-s-equity-stake-in-anchorage-digital"
    )


def test_tags_are_recovered_from_the_page_into_the_index(vault):
    assert _state(vault, STAKE).tags == ()
    repair(vault)
    assert _state(vault, STAKE).tags == (
        "research", "signal-derived", "stablecoins", "tether",
    )


def test_frontmatter_facts_are_recovered_into_the_index(vault):
    """Adoption left the dataclass defaults, so the index said `open` /
    `unverified` about a page that says `contested` / `single-source`. Anything
    rendered from the index would have stated the opposite of the page."""
    before = _state(vault, RESERVES)
    assert (before.research_status, before.confidence) == ("open", "unverified")
    repair(vault)
    after = _state(vault, RESERVES)
    assert after.research_status == "contested"
    assert after.confidence == "single-source"
    assert after.finding == "mixed"
    assert after.last_verified == "2026-07-26"


def test_a_frontmatter_value_outside_the_vocabulary_is_not_recovered(vault):
    """Left at the default and reported, rather than written into the index
    where a dashboard would repeat it."""
    path = vault / f"{QUANTUM}.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "research_status: open", "research_status: mostly settled"
        ),
        encoding="utf-8",
    )
    _resync(vault, path)
    repair(vault)
    assert _state(vault, "Quantum security claims").research_status == "open"


def test_related_links_are_computed_between_pages_that_share_tags(vault):
    """Both directions, and that is the point rather than thoroughness. Pages
    are repaired in glob order, so the FIRST page can only link to the last one
    if every page's tags reached the index before any page's links were
    computed."""
    repair(vault)
    stake = (vault / f"{STAKE}.md").read_text(encoding="utf-8")
    reserves = (vault / f"{RESERVES}.md").read_text(encoding="utf-8")
    assert f'related: ["[[{RESERVES}]]"]' in stake
    assert f'related: ["[[{STAKE}]]"]' in reserves


def test_a_page_sharing_no_tags_links_to_nothing(vault):
    """The counterweight: a link every page has is not a link."""
    repair(vault)
    assert "related: []" in (vault / f"{QUANTUM}.md").read_text(encoding="utf-8")


def test_a_page_is_never_renamed(vault):
    """The filename is the wikilink target."""
    before = sorted(p.name for p in vault.glob("*.md"))
    repair(vault)
    assert sorted(p.name for p in vault.glob("*.md")) == before


def test_a_repaired_page_stays_unmanaged(vault):
    """Repair recovers frontmatter facts. It does not recover the research, so
    append-only remains these pages' only update path."""
    repair(vault)
    assert all(not s.managed for s in VaultIndex(vault).load().all())


# --- blast radius -------------------------------------------------------------


def test_exactly_two_lines_of_a_page_change(vault):
    before = (vault / f"{STAKE}.md").read_text(encoding="utf-8").split("\n")
    repair(vault)
    after = (vault / f"{STAKE}.md").read_text(encoding="utf-8").split("\n")

    added = [line for line in after if line.startswith("topic_key:")]
    assert len(added) == 1
    remainder = [line for line in after if not line.startswith("topic_key:")]
    assert len(remainder) == len(before)
    differ = [i for i, (a, b) in enumerate(zip(before, remainder)) if a != b]
    assert len(differ) == 1, "repair changed a line other than related:"
    assert before[differ[0]].startswith("related:")


def test_repair_is_idempotent(vault):
    repair(vault)
    first = {p.name: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    assert repair(vault) == 0
    assert {p.name: p.read_bytes() for p in vault.rglob("*") if p.is_file()} == first


def test_a_crash_between_the_page_and_the_sidecar_re_runs_to_convergence(vault):
    """The page is written first, so a crash leaves a repaired page described by
    a pre-repair sidecar. Without the recovery precondition that state is
    permanently wedged: the hash no longer matches and every later run refuses."""
    path = vault / f"{STAKE}.md"
    key = _state(vault, STAKE).topic_key
    half_done = patch(path.read_text(encoding="utf-8"), topic_key=key, related=[])
    path.write_text(half_done, encoding="utf-8")   # sidecar deliberately NOT updated

    assert repair(vault) == 0
    assert _state(vault, STAKE).content_sha == content_hash(
        path.read_text(encoding="utf-8")
    )
    assert f"[[{RESERVES}]]" in path.read_text(encoding="utf-8")


def test_the_digest_stops_reporting_an_unknown_key(vault):
    """Triage may only reuse a key it has been shown. While the pages read
    `(key: -)` the same subject raised again opened a second page."""
    writer = VaultWriter(vault_dir=vault.parent, subdir=vault.name)
    assert "(key:" not in writer.digest(), "a key it was never shown does not exist"
    repair(vault)
    digest = writer.digest()
    assert digest.count("(key:") == 3, "every page is now updatable in place"


def _resync(vault_dir: Path, path: Path) -> None:
    """Point the sidecar at the bytes now on disk.

    Lets a test change one thing about a page without also tripping the
    hand-edit guard, which would make every such test pass for the wrong
    reason.
    """
    index_dir = vault_dir / ".srb-state"
    for sidecar in index_dir.glob("*.json"):
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        if raw["stem"] == path.stem:
            raw["content_sha"] = content_hash(path.read_text(encoding="utf-8"))
            sidecar.write_text(json.dumps(raw, indent=2), encoding="utf-8")
