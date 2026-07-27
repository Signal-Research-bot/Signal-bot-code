"""Recover the frontmatter facts adoption could not, on pages written before
the topic index existed.

    python -m signal_research_bot.kb.repair --dry-run
    python -m signal_research_bot.kb.repair

Explicitly invoked, never automatic -- same reasoning as `adopt`.

WHY THIS IS NOT A FLAG ON adopt.py
----------------------------------
`adopt` opens with a promise: it does not rename, rewrite, reformat or even open
a page for writing, and `git diff --name-status` after a real run shows sidecar
additions and nothing else. That promise is what makes it safe to run against a
live vault members read. This module breaks exactly that promise, so it is a
separate command with its own confirmation step rather than a mode of one whose
docstring says the opposite.

WHAT IS BROKEN, AND WHY IT MATTERS
----------------------------------
Adoption gave each legacy page a topic key in a sidecar. It could not put that
key ON the page, and it did not read anything out of the page -- so:

* `digest()` shows those pages to triage as `(key: -)`. Triage may only reuse a
  key it has been shown, so the same subject raised again opens a SECOND page
  next to the first. The one-page-per-topic guarantee does not hold for them.
* The sidecar says `research_status: open`, `confidence: unverified`,
  `finding: unestablished` and no dates, because those are the dataclass
  defaults, while the page itself says `answered` / `corroborated` /
  `supported`. Anything rendered from the index -- the dashboard -- would state
  the opposite of the page it links to.
* `tags: []` in the sidecar means `related_stems` sees no tags, so those pages
  can never link to anything and nothing can ever link to them.

TWO EDITS PER PAGE, AND NOTHING ELSE
------------------------------------
This is a line patch, not a re-render. One `topic_key:` line is inserted after
`entity_type:`, and the single `related:` line is replaced. Every other byte of
the page is carried through untouched, which is the property that makes the
precondition below survivable if it is ever wrong: the worst case is one
rewritten `related:` line, not a page replaced by a projection of state the bot
does not have. `managed` stays `False` -- append-only remains these pages' only
update path.

Fail-closed, per page. An anchor that is not present exactly once, a `tags:`
line that is not the exact shape `_yaml_list` writes, or an existing
`topic_key:` holding something else, and the page is left alone and reported.

PRECONDITION, AND CRASH RECOVERY
--------------------------------
A page is repaired only if its bytes hash to the sidecar's `content_sha`, OR it
already carries this page's own `topic_key:` line -- the state a crash between
the page write and the sidecar write leaves behind. Without the second case a
half-finished run wedges permanently; with it, re-running converges.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..config import Config, ConfigError
from ..logging_setup import configure
from .render import _TAG_SAFE, _yaml_list, _yaml_str
from .state import TopicState, VaultIndex, content_hash
from .writer import RESEARCH_SUBDIR, VaultError, VaultWriter

log = logging.getLogger(__name__)

_FENCE = "---"
_ANCHOR = "entity_type: research_task"
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Frontmatter scalars worth recovering, each with the exact set of values it is
# allowed to hold. Validated rather than trusted: these come off a file a person
# can edit, and a value outside the vocabulary is a signal that this page is not
# what this module thinks it is -- so it is left unrecovered, not written into
# the index where a dashboard would repeat it.
_RECOVERABLE: dict[str, frozenset[str] | re.Pattern[str]] = {
    "research_status": frozenset({"open", "researching", "answered", "contested", "dropped"}),
    "confidence": frozenset({"primary", "corroborated", "single-source", "unverified"}),
    "finding": frozenset({"supported", "refuted", "mixed", "unestablished"}),
    "first_raised": _ISO_DATE,
    "last_verified": _ISO_DATE,
}


@dataclass(frozen=True)
class PageRepair:
    """One page's plan. `refusal` set means nothing will be done to it."""

    path: Path
    topic_key: str = ""
    tags: tuple[str, ...] = ()
    facts: dict[str, str] = field(default_factory=dict)
    refusal: str | None = None


def _frontmatter_span(lines: list[str]) -> tuple[int, int] | None:
    """(first content line, closing fence index), or None if unrecognised."""
    if not lines or lines[0].strip() != _FENCE:
        return None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == _FENCE:
            return 1, i
    return None


def _parse_tags(value: str) -> tuple[str, ...] | None:
    """The exact shape `_yaml_list` writes, and nothing else.

    Deliberately not a YAML parser, for the reason state.py opens with: a
    lenient reader of human-edited YAML does not error on a shape it does not
    understand, it reads a wrong value and then writes that wrong value
    somewhere permanent. Here that would be a `related:` list computed from tags
    that are not the page's tags.
    """
    if value == "[]":
        return ()
    if not (value.startswith('["') and value.endswith('"]')):
        return None
    out: list[str] = []
    for item in value[2:-2].split('", "'):
        if not _TAG_SAFE.fullmatch(item):
            return None
        out.append(item)
    return tuple(out)


def _parse_links(value: str) -> tuple[str, ...]:
    """Wikilinks already on a `related:` line, in the exact shape we write.

    Anything else reads as no links rather than as an error: this feeds a union,
    so misreading a shape we do not recognise costs a duplicate at worst, never
    a deletion.
    """
    value = value.strip()
    if not value.startswith('["') or not value.endswith('"]'):
        return ()
    return tuple(item for item in value[2:-2].split('", "') if item)


def _carries_our_key(lines: list[str], span: tuple[int, int], topic_key: str) -> bool:
    return f"topic_key: {_yaml_str(topic_key)}" in lines[span[0]:span[1]]


def inspect(path: Path, state: TopicState) -> PageRepair:
    """Read one page and decide what, if anything, may be done to it."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    span = _frontmatter_span(lines)
    if span is None:
        return PageRepair(path, refusal="no frontmatter fences")

    if content_hash(text) != state.content_sha and not _carries_our_key(
        lines, span, state.topic_key
    ):
        # Either a person has been in it, or adoption recorded the hash of
        # different bytes. Both mean this module does not know what it is
        # holding, and kb-schema's "never edits a human-authored page" is not
        # being relaxed here any more than it is anywhere else.
        return PageRepair(path, refusal="edited since the bot last wrote it")

    start, end = span
    tags: tuple[str, ...] | None = None
    facts: dict[str, str] = {}
    for line in lines[start:end]:
        key, _, value = line.partition(": ")
        if key == "tags":
            if tags is not None:
                return PageRepair(path, refusal="more than one tags: line")
            tags = _parse_tags(value)
            if tags is None:
                return PageRepair(path, refusal="tags: line is not a shape we wrote")
        elif key in _RECOVERABLE and key not in facts:
            allowed = _RECOVERABLE[key]
            ok = value in allowed if isinstance(allowed, frozenset) else allowed.fullmatch(value)
            if ok:
                facts[key] = value

    if tags is None:
        return PageRepair(path, refusal="no tags: line")
    return PageRepair(path, topic_key=state.topic_key, tags=tags, facts=facts)


def patch(text: str, *, topic_key: str, related: list[str]) -> str | None:
    """The two edits, or None if the page does not present the exact anchors.

    Re-validated here rather than trusted from `inspect`, because this is the
    function that touches the file.
    """
    lines = text.split("\n")
    span = _frontmatter_span(lines)
    if span is None:
        return None
    start, end = span

    key_line = f"topic_key: {_yaml_str(topic_key)}"
    hits = [i for i in range(start, end) if lines[i].startswith("topic_key:")]
    if len(hits) > 1:
        return None
    if hits:
        if lines[hits[0]] != key_line:
            # Somebody else's key. Reassigning it would silently re-point every
            # future update at a different page.
            return None
    else:
        anchors = [i for i in range(start, end) if lines[i] == _ANCHOR]
        if len(anchors) != 1:
            return None
        lines.insert(anchors[0] + 1, key_line)
        end += 1

    hits = [i for i in range(start, end) if lines[i].startswith("related:")]
    if len(hits) != 1:
        return None
    # UNION, not replace. Computed tag-overlap links are one source of links on
    # a page; a link a person put there, or a migration added to connect this
    # page to a hand-written one, is another, and this function has no way to
    # tell them apart. Replacing would silently delete every link it did not
    # itself derive -- on a page whose whole purpose is to be connected.
    existing = _parse_links(lines[hits[0]].split(": ", 1)[1] if ": " in lines[hits[0]] else "")
    merged = list(existing) + [link for link in related if link not in existing]
    lines[hits[0]] = f"related: {_yaml_list(merged)}"
    return "\n".join(lines)


def plan(target_dir: Path, index: VaultIndex) -> list[PageRepair]:
    if not target_dir.is_dir():
        return []
    by_stem = {s.stem: s for s in index.all()}
    out: list[PageRepair] = []
    for path in sorted(target_dir.glob("*.md")):
        state = by_stem.get(path.stem)
        if state is None:
            out.append(PageRepair(path, refusal="no topic state; run kb.adopt first"))
        else:
            out.append(inspect(path, state))
    return out


def repair(target_dir: Path, *, dry_run: bool = False) -> int:
    """Returns 0 when every page was handled, 1 when any was refused."""
    index = VaultIndex(target_dir).load()
    plans = plan(target_dir, index)
    if not plans:
        print("nothing to repair: no pages found")
        return 0

    doable = [p for p in plans if p.refusal is None]
    refused = [p for p in plans if p.refusal is not None]

    # Every page's recovered tags go into the index BEFORE any page's `related`
    # is computed, or the first page in the glob links to nothing and the last
    # links to everything. Staged, not written: the sidecar is made durable in
    # the same step as the page it describes.
    for p in doable:
        index.stage(replace(index.get(p.topic_key), tags=p.tags, **p.facts))

    for p in plans:
        if p.refusal:
            print(f"  {p.path.name}\n    -> SKIPPED: {p.refusal}")
            continue
        related = index.related_stems(p.topic_key, p.tags)
        recovered = ", ".join(f"{k}={v}" for k, v in sorted(p.facts.items()))
        print(f"  {p.path.name}")
        print(f"    -> topic_key: {p.topic_key}")
        print(f"       tags: {len(p.tags)} recovered")
        print(f"       related: {', '.join(related) or '(none)'}")
        if recovered:
            print(f"       frontmatter: {recovered}")

    if dry_run:
        print(
            f"\n-- dry run: nothing written. {len(doable)} page(s) would be "
            f"repaired, {len(refused)} skipped --"
        )
        return 1 if refused else 0

    writer = VaultWriter(vault_dir=target_dir.parent, subdir=target_dir.name)
    changed = 0
    for p in doable:
        state = index.get(p.topic_key)
        text = p.path.read_text(encoding="utf-8")
        patched = patch(
            text, topic_key=p.topic_key,
            related=index.related_stems(p.topic_key, state.tags),
        )
        if patched is None:
            print(f"  {p.path.name}: anchors not found at write time; left untouched")
            refused.append(p)
            continue
        if patched != text:
            writer.write(p.path.stem, patched, overwrite=True)
            changed += 1
        # Written after the page, so a crash leaves the sidecar describing the
        # bytes that are actually on disk -- or the pre-repair bytes, which the
        # `_carries_our_key` recovery case then re-runs to convergence.
        index.put(replace(state, content_sha=content_hash(patched)))

    print(f"\nrepaired {len(doable)} page(s), {changed} rewritten, {len(refused)} skipped")
    return 1 if refused else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print what each page would get, write nothing")
    args = ap.parse_args()

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure(cfg.log_level)

    if not cfg.kb_dir:
        print("SRB_KB_DIR is unset; there is no vault to repair", file=sys.stderr)
        return 2
    try:
        return repair(cfg.kb_dir / RESEARCH_SUBDIR, dry_run=args.dry_run)
    except (VaultError, OSError) as exc:
        print(f"repair failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
