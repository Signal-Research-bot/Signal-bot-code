"""Render a knowledge-base record as Obsidian-flavoured Markdown.

Conventions follow .claude/skills/kb-schema/SKILL.md, with two deviations from
the operator's existing vault that are deliberate and documented there:

* `research_status`, never `status`. The existing vault already uses `status`
  for a deal-lifecycle vocabulary across 80+ pages; reusing the key poisons
  every query that spans both vaults.
* `confidence` is extended to carry `single-source` and `unverified`, which in
  the existing vault appear only in body text.

Pure functions: no filesystem, no clock, no network. The writer handles those.
"""

from __future__ import annotations

import re
from typing import Any

# Characters Obsidian and Windows will not accept in a filename.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")


def slug(title: str, max_len: int = 120) -> str:
    """A filename that is safe on Windows and stable as a wikilink target."""
    cleaned = _WS.sub(" ", _UNSAFE.sub("", title)).strip().rstrip(".")
    return (cleaned[:max_len].rstrip() or "Untitled")


def title_for(question: str, month: str) -> str:
    condensed = _WS.sub(" ", question).strip().rstrip("?")
    if len(condensed) > 80:
        condensed = condensed[:77].rstrip() + "..."
    return f"Research - {condensed} - {month}"


def _yaml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def frontmatter(record: dict[str, Any], *, title: str, first_raised: str,
                last_verified: str, related: list[str] | None = None) -> str:
    tags = sorted({"signal-derived", "research", *(record.get("tags") or [])})
    return "\n".join(
        [
            "---",
            f"title: {title}",
            "entity_type: research_task",
            f"research_status: {record['research_status']}",
            f"confidence: {record['confidence']}",
            f"first_raised: {first_raised}",
            f"last_verified: {last_verified}",
            f"tags: {_yaml_list(tags)}",
            f"sources: {_yaml_list([e['url'] for e in record.get('evidence') or []])}",
            f"related: {_yaml_list(related or [])}",
            "---",
        ]
    )


def _evidence_table(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "_No sources were retrieved for this entry._"
    rows = ["| Source | Quote | Confidence |", "| --- | --- | --- |"]
    for item in evidence:
        quote = item["quote"].replace("|", "\\|").replace("\n", " ").strip()
        if len(quote) > 300:
            quote = quote[:297] + "..."
        rows.append(f"| {item['url']} | {quote} | {item['confidence']} |")
    return "\n".join(rows)


def _bullets(items: list[str], empty: str) -> str:
    if not items:
        return empty
    return "\n".join(f"- {i}" for i in items)


def render(record: dict[str, Any], *, first_raised: str, last_verified: str,
           related: list[str] | None = None) -> tuple[str, str]:
    """Return (filename_stem, markdown)."""
    title = record["title"]
    parts = [
        frontmatter(
            record, title=title, first_raised=first_raised,
            last_verified=last_verified, related=related,
        ),
        "",
        f"# {title}",
        "",
    ]

    # A contested entry gets a callout at the top, matching the existing
    # vault's convention -- someone skimming must not read a disputed claim as
    # settled.
    if record["research_status"] == "contested":
        parts += [
            "> [!warning] Contested",
            "> Sources disagree on this. See Contradictions below before citing it.",
            "",
        ]

    parts += [
        "## Question",
        "",
        record["question"],
        "",
        "## Answer",
        "",
        record["answer"],
        "",
        "## Evidence",
        "",
        _evidence_table(record.get("evidence") or []),
        "",
        "## Contradictions",
        "",
        _bullets(record.get("contradictions") or [], "_None found._"),
        "",
        "## Open questions",
        "",
        _bullets(record.get("open_questions") or [], "_None recorded._"),
        "",
        "## Provenance",
        "",
        "Raised in the group chat and researched automatically. "
        "Participants are pseudonymised; see PRIVACY.md in the source repository.",
        "",
    ]
    return slug(title), "\n".join(parts)
