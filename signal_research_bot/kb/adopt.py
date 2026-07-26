"""Bring pages written before the topic index existed under management.

    python -m signal_research_bot.kb.adopt --dry-run
    python -m signal_research_bot.kb.adopt

Explicitly invoked, never automatic. A silent bulk touch of a live vault that
members read is exactly the class of change that should require someone to type
it, and `--dry-run` prints every key it would assign without writing anything.

WHAT ADOPTION DOES NOT DO
-------------------------
It does not rename, rewrite, reformat or even open a page for writing. It reads
each `.md`, takes the filename it already has, derives a topic key from it, and
writes a sidecar. `git diff --name-status` after a real run must show additions
under `.srb-state/` and nothing else -- that is the acceptance test, and the
one-line reason is that a page's filename is its wikilink target.

WHY THE SIDECARS COME OUT `managed: False`
------------------------------------------
The bot has no record of what is in these pages. It did not keep one, and this
module deliberately does not try to recover one by parsing them. So it must
never re-render them: an adopted page takes the append-only path forever, where
an update adds a dated block and bumps `last_verified` and touches nothing else.

Nothing here depends on adoption having been run. An un-adopted page whose
filename a new topic happens to want is caught by the collision guard in
`writer.write()`: reported, not clobbered.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ..config import Config, ConfigError
from ..logging_setup import configure
from .render import normalise_topic_key
from .state import TopicState, VaultIndex, content_hash
from .writer import RESEARCH_SUBDIR, VaultError

log = logging.getLogger(__name__)


def plan(target_dir: Path, index: VaultIndex) -> list[tuple[Path, str]]:
    """(page, topic_key) for every page not already in the index.

    Keys are derived from the filename, deduplicated against each other and
    against the index, so two pages that condense to one key still get distinct
    sidecars rather than one overwriting the other.
    """
    if not target_dir.is_dir():
        return []
    known_stems = index.stems()
    taken = index.keys()
    out: list[tuple[Path, str]] = []
    for path in sorted(target_dir.glob("*.md")):
        if path.stem in known_stems:
            continue
        key = normalise_topic_key(path.stem) or "topic"
        if key in taken:
            for n in range(2, 100):
                if f"{key}-{n}" not in taken:
                    key = f"{key}-{n}"
                    break
        taken.add(key)
        out.append((path, key))
    return out


def adopt(target_dir: Path, *, dry_run: bool = False) -> int:
    index = VaultIndex(target_dir).load()
    pending = plan(target_dir, index)

    if not pending:
        print("nothing to adopt: every page already has a topic key")
        return 0

    for path, key in pending:
        print(f"  {path.name}\n    -> topic_key: {key}")
    if dry_run:
        print(f"\n-- dry run: nothing written. {len(pending)} page(s) would be adopted --")
        return 0

    for path, key in pending:
        text = path.read_text(encoding="utf-8")
        index.put(
            TopicState(
                topic_key=key,
                stem=path.stem,          # the filename it already has. Never recomputed.
                title=path.stem,
                content_sha=content_hash(text),
                managed=False,
            )
        )
    print(f"\nadopted {len(pending)} page(s); no page was renamed or rewritten")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the key each page would get, write nothing")
    args = ap.parse_args()

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure(cfg.log_level)

    if not cfg.kb_dir:
        print("SRB_KB_DIR is unset; there is no vault to adopt", file=sys.stderr)
        return 2
    try:
        return adopt(cfg.kb_dir / RESEARCH_SUBDIR, dry_run=args.dry_run)
    except (VaultError, OSError) as exc:
        print(f"adoption failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
