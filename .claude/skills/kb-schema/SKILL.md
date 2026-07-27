---
name: kb-schema
description: Defines the Obsidian knowledge-base contract for generated research records — the frontmatter vocabulary, the research_status versus status collision with the operator's existing vault, the body section layout, and the rules about which files the bot may create or touch. Use this skill before editing anything under kb/, before changing frontmatter keys or values, before adding a Bases or Dataview view, and whenever a generated page needs a new field.
---

# Knowledge-base schema

Output is Markdown in the vault at `$SRB_KB_DIR` — a git repo and an Obsidian vault, kept **separate from** the operator's pre-existing research vault (`$SRB_FOREIGN_VAULT_DIR`, set so the bot can refuse to touch it). Never `git init` in the pre-existing vault, and never write into it.

Paths are always read from the environment. Hardcoding either location into a tracked file leaks the operator's directory layout — and `scrub_check.py`'s `absolute-windows-path` rule will block the commit.

Conventions below deliberately mirror CF so the two vaults feel like one system, with two intentional deviations called out.

## Frontmatter

```yaml
---
title: Tether's reserve attestations
entity_type: research_task          # alongside company / investment / source / person
topic_key: reserves-attestation     # stable join key; lowercase-hyphenated, never a date or a person
research_status: answered           # open | researching | answered | contested | dropped
finding: mixed                      # supported | refuted | mixed | unestablished
confidence: corroborated            # primary | corroborated | single-source | unverified
first_raised: 2026-07-24
last_verified: 2026-07-25
tags: ["signal-derived", "research"]
sources: ["https://example.org/filing"]   # raw URLs, not wikilinks
related: ["[[...]]"]
---
```

`sources:` holds the URLs themselves. An earlier version of this file showed
`[[Source - ...]]` wikilinks, which implied a `Sources and References/` page per
citation; nothing ever wrote one, so every entry in the list was a link to a
page that did not exist. The raw URL is what the evidence table already carries,
it is clickable in Obsidian and on GitHub alike, and it is what the grounding
check validates against.

### `research_status`, never `status`

`status:` already carries a **deal-lifecycle vocabulary** on 82 pages of the CF vault — `completed`, `closed`, `n/a`, `announced`, `held`, `outstanding`, plus free-text one-offs. Reusing that key for research state poisons every query that spans both vaults. This is the single easiest mistake to make here.

### `confidence` is a deliberate extension

In CF, the `confidence:` **frontmatter key** has only ever held `primary` (48 pages) and `corroborated` (40). The values `single-source` and `rumored` appear only in body text and evidence tables. Adding `single-source` and `unverified` to frontmatter is an intentional widening — apply it consistently from the first record, and never introduce a sixth value casually.

### Fields that must never appear

No `source_path`, no hostname, no local run ID, no absolute path, no message timestamp finer than 15 minutes. A member reading a generated page must learn nothing about the operator's machine. See `privacy-invariants`.

## Body layout

```markdown
# <title>

**<headline — the result, in one sentence>**

## Question
## Answer
## Evidence
| Source | Quote | Confidence |
## Contradictions
## Open questions
## Updates          # only once there has been one; append-only, oldest first
## Provenance       # fixed text: raised in chat, researched automatically
```

`Evidence` requires a **verbatim quote** per row, and every URL must come from the harvested search allowlist (see `claude-cascade`). `Contradictions` and `Open questions` are required sections — "none found" is an assertion worth recording, and an empty section is not the same as a missing one.

There is no `## Sources` section. It would restate the Evidence table's URLs and the `sources:` frontmatter key with nothing added, and a third copy is a third thing to fall out of sync.

## What the bot may and may not touch

- **Creates and updates its own pages**, under `Research Log/`, `Changelog/` and `Dashboard/`. (`Sources and References/` was in this list and never written to — every `sources:` entry is a raw URL, so there was never a per-citation page for it to hold.) One file per **topic**, not per run: a page is a living document keyed on `topic_key`, and later research on the same subject updates it in place. This said "Creates only. One file per record" until that rule was found to produce unlinked near-duplicates whenever the model phrased a title differently, and to silently discard the research whenever it phrased one the same.
- **Never edits a human-authored page.** Never edits a page it did not create. The bot stores a hash of the bytes it last wrote; if the file on disk differs, a person has been in it — the update is refused, logged, and recorded in the changelog for the operator to reconcile by hand. Unchanged, and load-bearing: the vault is a directory of notes a human also edits.
- **An update never renames.** The filename and `title:` are frozen at creation and read from the topic index, never recomputed from a later, differently-phrased title. They are the wikilink target (see Naming below).
- **An update is additive where it can be.** `last_verified` advances and the Answer carries the current finding; `sources`, `evidence`, `tags`, `contradictions` and `open questions` are unioned rather than replaced, `confidence` and `research_status` keep the stronger value, and every change appends a dated entry under `## Updates`. A recorded source or an unanswered question does not vanish because a later pass did not repeat it, and superseded prose stays recoverable in the vault's git history.
- Supersedes rather than rewrites **across topics**: a record that replaces a *different* topic's entry links to the one it replaces and sets the old one's `research_status: dropped`. Within one topic, a correction is an update, not a second page.
- Every create, update, refusal and collision is appended to `Changelog/YYYY-MM.md`, so what the bot did to the vault is auditable from inside Obsidian rather than only from the operator's local metrics file.
- `Dashboard/Research Overview.md` is **regenerated in full** on every run and carries a do-not-edit header. It is a projection of the topic index — status groups, a tag index, counts — and nothing reads it back. It must contain **no clock**: a generated-on line would change the file every run, so the batch would commit and push a revision of it on every window and the vault history would stop being a record of what changed. Regenerate, byte-compare, write only on a difference.
- Writes atomically — temp file plus `os.replace()` — then commits. A half-written page in an Obsidian vault is visible immediately to anyone with the repo open.
- Retracts on `remoteDelete` / `editMessage` upstream (see `signal-envelope`); the KB is not append-only-forever.

## Obsidian plugin reality in this vault family

- **Dataview is installed but NOT enabled** in CF. Do not write pages that depend on Dataview queries rendering. The second reason is stronger than the first and applies even if it were enabled: the archive is shared with members through a git host, where a Dataview block shows the reader the query rather than the answer. `Dashboard/Research Overview.md` is the portable answer to the same need.
- **Bases is enabled but there are zero `.base` files** — it is entirely unexercised. Author and test one `.base` view before betting the schema on it. A `.base` is a good fit for the operator's own ad-hoc slicing and a bad fit for the shared artefact, for the same reason as Dataview. Anything the bot writes must render as plain Markdown; anything the operator hand-authors may use whatever they like, and the bot must never touch it.
- Keep frontmatter **flat and portable**. Bases supports nested access (`property.subprop`), so nesting is not forbidden by the tool — flat is chosen for portability across both vaults and for legibility to members reading raw Markdown.
- Wikilinks, the graph view, and `> [!warning]` callouts for contested claims are all in active use in CF. Match them.

## Naming

**The bare subject, condensed**: `Tether's equity stake in Anchorage Digital`, `Has Tether provided verifiable proof of reserves`. No prefix, no date.

This is a deliberate divergence from CF's `Company - X` / `Investment - X in Y - YYYY-MM` pattern, and from what this file used to say. Three reasons:

- A date in the filename says when the page was *opened*, and a page is now a living document whose newest content may be months later. `first_raised` and `last_verified` carry that honestly; a stale date in the wikilink target does not.
- The prefix is redundant with `entity_type: research_task` and with the directory, and it pushes the distinguishing words past where a filename is legible in a sidebar or a graph node.
- Members read these. `[[Tether's equity stake in Anchorage Digital]]` reads as a sentence; `[[Research - Tether's equity stake in Anchorage... - 2026-07]]` does not.

`Research - <condensed question> - YYYY-MM` survives as the **fallback only**, for a record that arrives with no title at all (`title_for` in render.py). It should be rare enough to be a signal.

Keep titles stable — they are the wikilink target, and renaming breaks inbound links across both vaults.

`topic_key`, not the title, is what joins later research to an existing page. So a page keeps the filename it was created with even when the same subject is raised again and phrased differently — the key matches, the title is discarded, and the link target survives.
