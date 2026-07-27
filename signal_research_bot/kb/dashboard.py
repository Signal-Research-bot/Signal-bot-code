"""One page that indexes every topic in the archive.

WHY A GENERATED PAGE AND NOT A QUERY
------------------------------------
Obsidian's Dataview plugin would do this live, in one code block. It is not used
here, and that is a decision rather than an omission: `.claude/skills/kb-schema`
records that Dataview is installed but NOT enabled in this vault family, so a
page depending on it renders as its own source code. More importantly, the
archive is shared with group members through a git host, and a Dataview block is
invisible there -- the reader sees the query, not the answer. A static page is
legible in Obsidian, on GitHub, and in a plain text editor, which is the whole
point of an archive other people are meant to be able to read.

The cost is that it has to be regenerated, which is what `write_dashboard` is
for.

WHY THERE IS NO CLOCK IN THIS MODULE
------------------------------------
`render_dashboard` takes the index and nothing else. A "generated on" line would
change every run, so the file would differ every run, so the batch would commit
and push a new revision of it on every window regardless of whether a single
topic had moved. Determinism here is not tidiness -- it is what keeps the vault
history readable. The dates that matter are each topic's own `last_verified`,
which is on the page and in the index.
"""

from __future__ import annotations

from .render import depersonalise
from .state import VaultIndex
from .writer import VaultWriter

DASHBOARD_SUBDIR = "Dashboard"
DASHBOARD_STEM = "Research Overview"

# Reading order, not the precedence order. `_STATUS_ORDER` in state.py is sorted
# by stickiness -- `dropped` first, because a superseded page must not silently
# revert -- which is exactly the wrong thing to open a dashboard with. Anything
# not listed here still gets its own section (see `_grouped`), so a sixth status
# added to the schema cannot silently vanish from the index.
_DISPLAY_ORDER = ("contested", "open", "researching", "answered", "dropped")

_HEADINGS = {
    "contested": "Contested",
    "open": "Open",
    "researching": "In progress",
    "answered": "Answered",
    "dropped": "Superseded",
}

# On every page by construction, so they carry no navigational information --
# the same exclusion `related_stems` makes, for the same reason.
_BOILERPLATE_TAGS = frozenset({"signal-derived", "research"})

_HEADER = "\n".join([
    "---",
    "title: Research Overview",
    "entity_type: dashboard",
    'tags: ["signal-derived", "dashboard"]',
    "---",
    "",
    "# Research Overview",
    "",
    "Every topic in this archive, grouped by status. Written automatically by "
    "the research bot and rewritten on every run — edit a topic's own page, not "
    "this one.",
    "",
])


def _grouped(index: VaultIndex) -> list[tuple[str, list]]:
    """(status, states) in reading order, with unknown statuses appended."""
    by_status: dict[str, list] = {}
    for state in index.all():
        by_status.setdefault(state.research_status, []).append(state)
    order = list(_DISPLAY_ORDER) + sorted(set(by_status) - set(_DISPLAY_ORDER))
    return [(s, sorted(by_status[s], key=lambda x: x.stem)) for s in order if s in by_status]


def _entry(state) -> str:
    verified = state.last_verified or "not recorded"
    return (
        f"- [[{state.stem}]] — {state.finding or 'unestablished'}, "
        f"{state.confidence or 'unverified'}, last verified {verified}"
    )


def _tag_index(index: VaultIndex) -> list[str]:
    """Tag -> pages, for the tags that actually connect two or more topics.

    Single-page tags are counted rather than listed. A tag on one page is
    already reachable from that page and from Obsidian's tag pane; listing every
    one of them would make this section longer than the rest of the file
    combined and grow without bound. The count is stated so the omission is
    visible rather than looking like completeness.
    """
    pages: dict[str, list[str]] = {}
    for state in index.all():
        for tag in set(state.tags) - _BOILERPLATE_TAGS:
            pages.setdefault(tag, []).append(state.stem)

    shared = {t: sorted(s) for t, s in pages.items() if len(set(s)) > 1}
    lines = [
        f"- **{tag}** — " + ", ".join(f"[[{stem}]]" for stem in stems)
        for tag, stems in sorted(shared.items())
    ]
    lonely = len(pages) - len(shared)
    if lonely:
        lines.append(
            f"- _{lonely} further tag(s) appear on a single page each and are "
            f"listed on those pages._"
        )
    return lines or ["_No topics are tagged yet._"]


def render_dashboard(index: VaultIndex) -> str:
    """The whole page. Pure: same index in, same bytes out, forever."""
    groups = _grouped(index)
    total = sum(len(states) for _, states in groups)

    parts = [_HEADER]
    if not total:
        parts += ["_The archive is empty._", ""]
    for status, states in groups:
        heading = _HEADINGS.get(status, status.replace("-", " ").capitalize())
        parts += [f"## {heading} ({len(states)})", ""]
        parts += [_entry(s) for s in states]
        parts += [""]

    parts += ["## Topics by tag", ""]
    parts += _tag_index(index)
    parts += ["", "## Counts", "", "| Status | Topics |", "| --- | --- |"]
    parts += [f"| {status} | {len(states)} |" for status, states in groups]
    parts += [f"| **total** | **{total}** |", ""]

    # Applied to the finished page rather than to each field. Stems on adopted
    # pages were never depersonalised by anything -- they are filenames from
    # before the index existed -- and this is the last point before a
    # member-readable file. A wikilink rewritten here would fail to resolve,
    # which is a visible, harmless breakage; a stable speaker label sitting in
    # the archive's index page is neither.
    return depersonalise("\n".join(parts))


def write_dashboard(vault: VaultWriter, index: VaultIndex) -> bool:
    """Regenerate the dashboard. True only when the bytes actually changed.

    The byte-compare is load-bearing. `_finish` commits when anything was
    written, so a dashboard rewritten unconditionally would produce a commit and
    a push on every window -- including windows that researched nothing -- and
    the vault history would stop being a record of what changed.

    Compared by reading the file rather than by probing with `overwrite=False`:
    a probe that hits an existing page logs a collision at ERROR, which is the
    signal reserved for research that was lost.
    """
    writer = VaultWriter(
        vault.vault_dir, vault.foreign_vault_dir, subdir=DASHBOARD_SUBDIR
    )
    markdown = render_dashboard(index)
    path = writer.target_dir / f"{DASHBOARD_STEM}.md"
    if path.exists() and path.read_text(encoding="utf-8") == markdown:
        return False
    return writer.write(DASHBOARD_STEM, markdown, overwrite=True).wrote
