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
import unicodedata
from hashlib import sha256
from typing import Any

# Characters Obsidian and Windows will not accept in a filename.
#
# `#`, `[`, `]` and `^` are here because they break an Obsidian [[wikilink]]
# even though Windows accepts them in a filename: a page whose name contains
# one cannot be linked to, which matters now that pages link to each other.
_UNSAFE = re.compile(r'[<>:"|?*\[\]#^\x00-\x1f]')

# Path separators, handled separately because DELETING one joins two words that
# were never one word. Three of the five live pages are named "TreasuryFed",
# "BlackRockAnchorage" and "ZaguryElektronPeak" -- each was "Treasury/Fed",
# "BlackRock/Anchorage", "Zagury/Elektron/Peak" before this ran. The filename is
# the permanent wikilink target, so a mangled one is mangled forever.
_SEPARATOR = re.compile(r"[/\\]")

# Every page the bot writes is named "Research - <subject>", matching the
# "Type - Subject" grammar every page in the operator's vault already follows
# (Company - X, Investment - X in Y - YYYY-MM, Person - X, Source - X - date).
#
# It is also a namespace guard. Without it a model that titled a page "Company -
# Anchorage Digital" would aim at the filename of a page a human wrote; the
# writer would refuse and report a collision, which is safe but is a research
# task lost to a naming accident.
RESEARCH_PREFIX = "Research - "

_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# A value that cannot change YAML's meaning: no quotes, colons, brackets,
# commas, comment markers, indicators or leading/trailing space. Enum values
# and ISO dates match; a model-authored title does not.
_BARE_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Speaker labels, as the transcript presents them to the model.
_SPEAKER = re.compile(r"(?<!\w)Participants?\s+[A-Z]{1,3}(?!\w)")
# "@handle" written by a member and copied through by the model.
#
# The lookbehind excludes "/" as well as word characters, and URLs are carved
# out entirely below. Without both, this fired on the "/@user" segment of a
# link: "https://x.com/@coinmetrics/status/1" became
# "https://x.com/a group member/status/1". That corrupted evidence URLs -- the
# whole point of the archive -- and the mangled URL then failed the grounding
# check, so the citation was dropped as a fabrication too.
_AT_HANDLE = re.compile(r"(?<![\w@/])@[A-Za-z0-9._-]{2,}")
_URL = re.compile(r"https?://\S+", re.I)

MEMBER = "a group member"

# A topical slug: lowercase, hyphenated, no '@', no underscores, no digits-only.
# Deliberately excludes the shapes handles usually take.
_TAG_SAFE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

# A speaker label that has already been slugified ("participant-b"). depersonalise
# only matches the prose form, "Participant B", so a model that writes a topic
# key in slug form would slip a stable label past it and into a filename.
_SPEAKER_SLUG = re.compile(r"(?<![a-z0-9])participants?-[a-z]{1,3}(?![a-z0-9])")

TOPIC_KEY_MAX = 64

# Windows refuses these as filenames whatever the extension -- "nul.md" is not
# a file. They match the topical-slug shape, so they have to be excluded by
# name rather than by charset.
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _scrub(text: str) -> str:
    return _AT_HANDLE.sub(MEMBER, _SPEAKER.sub(MEMBER, text))


def depersonalise(value: Any) -> Any:
    """Strip participant references from model-authored text.

    Runs on the record just before it becomes a permanent page, and again
    before anything is posted back to the group.

    Why this exists at all. The pipeline is careful that no *identity* reaches
    the model, but the labels it substitutes instead are stable for the life of
    the archive -- `Participant B` is the same person in every entry, forever.
    Nothing stopped the model writing "Participant B was wrong about the
    reserves" into a page, and the egress firewall accepts it by design, since
    an allocated label is exactly what it is supposed to allow. In a group of
    eight, a label plus the subject matter is often enough for members to work
    out who is meant. The operator's ask was that user information stay out of
    the research itself, and an attributed claim in a permanent, member-readable
    file is precisely that.

    So the labels are removed from the *output* rather than the input: they are
    genuinely useful to the model while it reasons about a conversation, and
    carry nothing once the finding is written down. A research page should read
    as a claim about the world, not a claim about who said what.

    Strips rather than blocks. A page with "a group member" in it is fine; a
    window that fails because a label appeared is not, and the operator has
    said plainly that a working tool matters more.
    """
    if isinstance(value, str):
        # URLs are carved out and passed through untouched. A source link is
        # evidence, not prose, and rewriting any part of it both corrupts the
        # citation and makes it fail the grounding check downstream.
        parts = []
        last = 0
        for match in _URL.finditer(value):
            parts.append(_scrub(value[last:match.start()]))
            parts.append(match.group())
            last = match.end()
        parts.append(_scrub(value[last:]))
        return "".join(parts)
    if isinstance(value, list):
        return [depersonalise(v) for v in value]
    if isinstance(value, dict):
        return {k: depersonalise(v) for k, v in value.items()}
    return value


def slug(title: str, max_len: int = 120) -> str:
    """A filename that is safe on Windows and stable as a wikilink target."""
    cleaned = _UNSAFE.sub("", _SEPARATOR.sub("-", title))
    # Collapse and trim the substituted hyphens, or a title of pure separators
    # becomes a file literally named "---.md".
    cleaned = re.sub(r"-{2,}", "-", _WS.sub(" ", cleaned)).strip(" -.")
    return (cleaned[:max_len].strip(" -.") or "Untitled")


def research_title(title: str) -> str:
    """The vault's "Type - Subject" grammar, applied to a model-authored title."""
    text = _WS.sub(" ", str(title or "")).strip()
    if not text:
        return RESEARCH_PREFIX.rstrip(" -")
    if text.lower().startswith(RESEARCH_PREFIX.lower()):
        return text
    return RESEARCH_PREFIX + text


def normalise_topic_key(value: Any) -> str | None:
    """Coerce a model-authored topic key to a safe slug, or None if nothing survives.

    The key joins new research to an existing page, so it ends up in a filename
    lookup, in frontmatter, and in the digest the model is shown next window.
    Three properties matter, and all three are enforced here rather than in the
    schema -- structured outputs do not support `pattern` or `minLength`, and a
    constraint the API strips is not a constraint at all. `tags` is handled the
    same way for the same reason.

    * **No traversal, by construction.** The output alphabet has no `.`, `/`,
      `\\` or `:`, so a key cannot address a path outside the vault however it
      was authored. That is stronger than a check someone can forget to call.
    * **No participant.** A key is permanent and member-visible. `depersonalise`
      catches "Participant B"; the slug pattern below catches "participant-b",
      which it would not.
    * **Readable.** The operator reads these while skimming the vault, and
      `privacy-invariants` forbids a UUID-shaped string in a tracked file, so a
      hash is out on both counts.
    """
    text = depersonalise(str(value or ""))
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = _SPEAKER_SLUG.sub("-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    # A key must start with a letter: that keeps it clear of digits-only values
    # and of anything that reads as a date.
    text = text.lstrip("0123456789-")
    if len(text) > TOPIC_KEY_MAX:
        head = text[:TOPIC_KEY_MAX]
        # Truncate at a hyphen so the key stays a whole word, not a fragment.
        text = (head.rsplit("-", 1)[0] if "-" in head else head).strip("-")
    if not text or text in _RESERVED_STEMS or not _TAG_SAFE.fullmatch(text):
        return None
    return text


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


# The grades the operator's vault records in the `confidence:` KEY. It is an
# evidence-grade rating tied to source type, used on 88 hand-written pages, and
# it has only ever held these two values.
#
# The weaker grades exist in that vault too -- as TAGS: `unverified` on 54
# pages, `single-source` on 35. 184 pages carry no `confidence:` key at all.
# That is not an omission, it is the convention: the key asserts a positive
# evidence grade, and its absence plus a tag says the evidence did not reach
# one.
#
# So the bot follows it. Writing four values into a key that has held two would
# make the field ambiguous across the whole corpus with no way to tell
# afterwards which scale any given page used -- and the vault's own Dashboard
# has a "Confidence Breakdown" table reading exactly that key. A page's real
# grade is never lost: it stays in the sidecar, where `_CONFIDENCE_ORDER` still
# ranks all four for the stronger-value-wins merge. The divergence is at the
# render boundary only.
_FRONTMATTER_CONFIDENCE = frozenset({"primary", "corroborated"})

# Near-misses that would fragment a shared tag pane: two spellings of one idea
# produce two tags, two filtered views, and a reader who finds half the pages.
# Left-hand side is what a model reaches for, right-hand side is what the vault
# already uses.
_TAG_CANONICAL = {
    "stablecoins": "stablecoin",
    "bitcoin-mining": "mining",
    "crypto-custody": "custody",
    "conflicts-of-interest": "conflict-of-interest",
    "related-party-transactions": "related-party",
    "institutional-finance": "institutional",
    "corporate-ownership": "ownership",
}


def frontmatter(record: dict[str, Any], *, title: str, first_raised: str,
                last_verified: str, related: list[str] | None = None,
                topic_key: str = "") -> str:
    # Filtered on shape, not just trusted from the schema. An Obsidian tag is a
    # clickable index across the whole vault, so a handle landing here would
    # build a browsable page-set per person -- a worse outcome than the same
    # string sitting in body text. Anything that is not a plain topical slug is
    # dropped rather than sanitised, since a mangled tag is no use to anyone.
    supplied = [
        _TAG_CANONICAL.get(str(t), str(t))
        for t in (record.get("tags") or [])
        if _TAG_SAFE.fullmatch(str(t))
    ]
    confidence = str(record["confidence"])
    weak_grade = [] if confidence in _FRONTMATTER_CONFIDENCE else [confidence]
    tags = sorted({"signal-derived", "research", *supplied, *weak_grade})

    lines = [
        "---",
        f"title: {_yaml_str(title)}",
        "entity_type: research_task",
        # On the page, not only in the index: it makes the index rebuildable
        # from the vault alone with the same prefix scan digest() already
        # does, so losing var/ or a sidecar costs nothing.
        f"topic_key: {_yaml_str(topic_key)}",
        f"research_status: {_yaml_str(record['research_status'])}",
        f"finding: {_yaml_str(record.get('finding', 'unestablished'))}",
    ]
    if confidence in _FRONTMATTER_CONFIDENCE:
        lines.append(f"confidence: {_yaml_str(confidence)}")
    lines += [
        f"first_raised: {_yaml_str(first_raised)}",
        f"last_verified: {_yaml_str(last_verified)}",
        f"tags: {_yaml_list(tags)}",
        f"sources: {_yaml_list([e['url'] for e in record.get('evidence') or []])}",
        f"related: {_yaml_list(related or [])}",
        "---",
    ]

    block = "\n".join(lines)
    # `status:` carries a deal-lifecycle vocabulary on 88 hand-written pages --
    # `completed`, `closed`, `announced`, and free text like "exited 2026-03-24
    # at $10.80/share". Emitting research state into that key would corrupt
    # every page and every table that reads it, corpus-wide, with no way to tell
    # afterwards which pages meant which thing. Nothing above can produce the
    # line today; this is here so nothing added later can either.
    assert "\nstatus:" not in block, "the bot must never write the vault's status: key"
    return block


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


def bump_last_verified(text: str, date: str) -> str | None:
    """Advance `last_verified` in an existing page. None if it cannot be done safely.

    For pages the bot wrote before it kept a record of their contents: it cannot
    re-render them, so this changes one line and nothing else.

    Deliberately not a parser. It matches one exact shape -- an unquoted ISO
    date, on its own line, inside the frontmatter fences -- and refuses anything
    else rather than guessing. A quoted value, a missing key, or a `last_verified`
    in body text all return None, and the caller records the skip. The whole
    reason this module never reads a page back is that a lenient reader of
    human-edited YAML writes a wrong value into a permanent page.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    hits = [
        i for i in range(1, end)
        if re.fullmatch(r"last_verified: \d{4}-\d{2}-\d{2}", lines[i])
    ]
    if len(hits) != 1:
        return None
    lines[hits[0]] = f"last_verified: {date}"
    return "\n".join(lines)


def render_update_block(update: dict[str, Any]) -> str:
    """One dated entry for a page's Updates section.

    Depersonalises internally, and is the ONLY function that formats an update.
    Both render() and the legacy append path call it, so there is no second
    place where member-readable text is built. That matters more than it looks:
    every depersonalisation guarantee in this codebase is a property of
    render(), not of the writer, which takes an opaque string and writes bytes.
    A path that formatted its own update block would bypass the strip entirely
    -- and every existing test would stay green, because they all assert on
    render()'s return value and none of them reads a file back.
    """
    update = depersonalise(update)
    parts = [f"### {_cell(update.get('date', ''))}", ""]

    headline = str(update.get("headline") or "").strip()
    if headline:
        parts += [headline, ""]

    notes = [str(c).strip() for c in (update.get("changes") or []) if str(c).strip()]
    if notes:
        parts += [_bullets(notes, ""), ""]

    sources = [str(u).strip() for u in (update.get("sources") or []) if str(u).strip()]
    if sources:
        parts += ["Sources added:", "", _bullets(sources, ""), ""]
    return "\n".join(parts).rstrip() + "\n"


def render(record: dict[str, Any], *, first_raised: str, last_verified: str,
           related: list[str] | None = None, topic_key: str = "",
           updates: list[dict[str, Any]] | None = None,
           stem: str | None = None,
           hubs: tuple[str, ...] = ()) -> tuple[str, str]:
    """Return (filename_stem, markdown).

    `stem` freezes the filename. An update passes the stem the page was created
    with, because the filename is the wikilink target and renaming it breaks
    every inbound link; only a create derives it from the title.

    `hubs` are the vault's index pages -- the ones nearly every hand-written
    page links back to at the foot. That convention is why the operator's vault
    has two orphans in 272, and a generated page that skips it is an orphan by
    construction: nothing links to it and it links to nothing outside its own
    tag cluster. Passed in rather than read from the environment, because the
    hub names are a property of one vault and this module is a pure function.
    """
    # Applied to EVERYTHING before anything is read out of it, so a field added
    # to the schema later cannot bypass it by being rendered somewhere this
    # function does not currently look. Covers the page body, the frontmatter,
    # and -- via slug(title) -- the filename.
    #
    # `related` and `updates` are inside the same payload rather than
    # depersonalised separately: as plain keyword arguments they went straight
    # to _yaml_list and the body, which was a live bypass the moment either
    # stopped being empty.
    payload = depersonalise({
        "record": record,
        "related": list(related or []),
        "updates": list(updates or []),
        "topic_key": topic_key,
    })
    record = payload["record"]
    related = payload["related"]
    updates = payload["updates"]
    topic_key = payload["topic_key"]

    # Collapsed once, here, so the frontmatter scalar, the H1 and the filename
    # all agree. A multi-line title otherwise produces a heading that silently
    # continues into body text.
    raw_title = _WS.sub(" ", str(record.get("title") or "")).strip()
    title = research_title(raw_title or "Untitled")

    # Depersonalisation collapses distinct titles onto one filename: "Research -
    # Participant A on reserves" and "... Participant B on reserves" both become
    # "Research - a group member on reserves". VaultWriter treats an existing
    # path as idempotency and skips the write, so the second entry was silently
    # discarded while the batch counted it as written and announced it to the
    # group.
    #
    # A short digest of the question disambiguates, and only when something was
    # actually stripped -- ordinary titles keep clean, readable filenames.
    if stem is None:
        stem = slug(title)
        if MEMBER in title:
            digest = sha256(
                str(record.get("question", "")).encode("utf-8")
            ).hexdigest()[:6]
            stem = slug(f"{title} ({digest})")
    # Computed once and used in both places: `related:` frontmatter and the
    # `## Related Pages` block at the foot of the page. The vault carries the
    # same links in both, and two lists that disagree is worse than either.
    backlinks = list(related) + [f"[[{h}]]" for h in hubs if f"[[{h}]]" not in related]

    parts = [
        frontmatter(
            record, title=title, first_raised=first_raised,
            last_verified=last_verified, related=backlinks, topic_key=topic_key,
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

    if updates:
        # Plain text, not a Dataview query: Dataview is installed but not
        # enabled in this vault family, so anything depending on it renders as
        # source. See .claude/skills/kb-schema.
        parts += [f"*Revised {updates[-1].get('date', '')} — see Updates below.*", ""]

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
    ]

    if updates:
        # Append-only, oldest first, so the section reads chronologically and a
        # reader sees how the finding moved rather than only where it landed.
        parts += ["## Updates", ""]
        for entry in updates:
            parts += [render_update_block(entry), ""]

    parts += [
        "## Provenance",
        "",
        "Raised in the group chat and researched automatically. "
        "Participants are pseudonymised; see PRIVACY.md in the source repository.",
        "",
    ]

    # Last block on the page, matching the convention 271 of the 272 hand-written
    # pages follow. The same links are in `related:` frontmatter; the vault
    # duplicates them in the body and so does this, because that is what the
    # vault does and a generated page that reads differently reads as an
    # intruder. The hubs are appended so the page is reachable from the
    # dashboard and the timeline rather than only from its own tag cluster.
    if backlinks:
        parts += ["## Related Pages", "", *[f"- {link}" for link in backlinks], ""]

    return stem, "\n".join(parts)
