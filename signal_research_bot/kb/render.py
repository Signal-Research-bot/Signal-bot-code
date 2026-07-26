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
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# A value that cannot change YAML's meaning: no quotes, colons, brackets,
# commas, comment markers, indicators or leading/trailing space. Enum values
# and ISO dates match; a model-authored title does not.
_BARE_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def slug(title: str, max_len: int = 120) -> str:
    """A filename that is safe on Windows and stable as a wikilink target."""
    cleaned = _WS.sub(" ", _UNSAFE.sub("", title)).strip().rstrip(".")
    return (cleaned[:max_len].rstrip() or "Untitled")


def title_for(question: str, month: str) -> str:
    condensed = _WS.sub(" ", question).strip().rstrip("?")
    if len(condensed) > 80:
        condensed = condensed[:77].rstrip() + "..."
    return f"Research - {condensed} - {month}"


def _yaml_str(value: Any) -> str:
    """One YAML scalar, safe for any input.

    Every value in the frontmatter block below is model-authored, and the model
    is summarising attacker-controlled chat. Interpolating those straight into
    `f"title: {title}"` let a newline in the title close the scalar and open
    arbitrary further keys -- so a member could dictate frontmatter on a page in
    a repo other members read, including keys that change how Obsidian renders
    it. Escaping here rather than at each call site means a new field cannot
    forget to do it.

    Simple tokens (`answered`, `2026-07-25`) are emitted bare, because the
    operator's existing vault writes them that way and a quoted enum reads as a
    different value to a human skimming the file. Anything else is quoted.
    """
    text = _CONTROL.sub("", _WS.sub(" ", str(value)).strip())
    if _BARE_SAFE.fullmatch(text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    # List items are always quoted: the existing vault quotes them, and a bare
    # item containing `,` or `]` would change the list's length rather than its
    # contents -- a failure that reads as valid YAML.
    return "[" + ", ".join(
        '"' + _CONTROL.sub("", _WS.sub(" ", str(v)).strip())
        .replace("\\", "\\\\").replace('"', '\\"') + '"'
        for v in values
    ) + "]"


def frontmatter(record: dict[str, Any], *, title: str, first_raised: str,
                last_verified: str, related: list[str] | None = None) -> str:
    tags = sorted({"signal-derived", "research", *(record.get("tags") or [])})
    return "\n".join(
        [
            "---",
            f"title: {_yaml_str(title)}",
            "entity_type: research_task",
            f"research_status: {_yaml_str(record['research_status'])}",
            f"finding: {_yaml_str(record.get('finding', 'unestablished'))}",
            f"confidence: {_yaml_str(record['confidence'])}",
            f"first_raised: {_yaml_str(first_raised)}",
            f"last_verified: {_yaml_str(last_verified)}",
            f"tags: {_yaml_list(tags)}",
            f"sources: {_yaml_list([e['url'] for e in record.get('evidence') or []])}",
            f"related: {_yaml_list(related or [])}",
            "---",
        ]
    )


def _cell(value: Any) -> str:
    """One Markdown table cell. A raw `|` or newline would break the table."""
    return _CONTROL.sub("", _WS.sub(" ", str(value)).replace("|", "\\|")).strip()


def _evidence_table(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "_No sources were retrieved for this entry._"
    rows = ["| Source | Quote | Confidence |", "| --- | --- | --- |"]
    for item in evidence:
        quote = _cell(item["quote"])
        if len(quote) > 300:
            quote = quote[:297] + "..."
        rows.append(f"| {_cell(item['url'])} | {quote} | {_cell(item['confidence'])} |")
    return "\n".join(rows)


def _bullets(items: list[str], empty: str) -> str:
    if not items:
        return empty
    return "\n".join(f"- {i}" for i in items)


def render(record: dict[str, Any], *, first_raised: str, last_verified: str,
           related: list[str] | None = None) -> tuple[str, str]:
    """Return (filename_stem, markdown)."""
    # Collapsed once, here, so the frontmatter scalar, the H1 and the filename
    # all agree. A multi-line title otherwise produces a heading that silently
    # continues into body text.
    title = _WS.sub(" ", str(record["title"])).strip() or "Untitled"
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

    headline = (record.get("headline") or "").strip()
    if headline:
        # The answer, first thing on the page. Someone skimming the vault
        # should not have to read three sections to learn the result.
        parts += [f"**{headline}**", ""]

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
