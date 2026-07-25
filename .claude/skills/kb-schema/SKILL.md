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
title: Research - <question, condensed> - 2026-07-25
entity_type: research_task          # alongside company / investment / source / person
research_status: answered           # open | researching | answered | contested | dropped
confidence: corroborated            # primary | corroborated | single-source | unverified
first_raised: 2026-07-24
last_verified: 2026-07-25
tags: ["signal-derived", "research"]
sources: ["[[Source - ...]]"]
related: ["[[...]]"]
---
```

### `research_status`, never `status`

`status:` already carries a **deal-lifecycle vocabulary** on 82 pages of the CF vault — `completed`, `closed`, `n/a`, `announced`, `held`, `outstanding`, plus free-text one-offs. Reusing that key for research state poisons every query that spans both vaults. This is the single easiest mistake to make here.

### `confidence` is a deliberate extension

In CF, the `confidence:` **frontmatter key** has only ever held `primary` (48 pages) and `corroborated` (40). The values `single-source` and `rumored` appear only in body text and evidence tables. Adding `single-source` and `unverified` to frontmatter is an intentional widening — apply it consistently from the first record, and never introduce a sixth value casually.

### Fields that must never appear

No `source_path`, no hostname, no local run ID, no absolute path, no message timestamp finer than 15 minutes. A member reading a generated page must learn nothing about the operator's machine. See `privacy-invariants`.

## Body layout

```markdown
# <title>

## Question
## Answer
## Evidence
| Source | Quote | Confidence |
## Contradictions
## Open questions
## Sources
```

`Evidence` requires a **verbatim quote** per row, and every URL must come from the harvested search allowlist (see `claude-cascade`). `Contradictions` and `Open questions` are required sections — "none found" is an assertion worth recording, and an empty section is not the same as a missing one.

## What the bot may and may not touch

- **Creates only**, under `Research Log/` and `Sources and References/`. One file per record.
- **Never edits a human-authored page.** Never edits a page it did not create.
- Supersedes rather than rewrites: a corrected record links to the one it replaces and sets the old one's `research_status: dropped`.
- Writes atomically — temp file plus `os.replace()` — then commits. A half-written page in an Obsidian vault is visible immediately to anyone with the repo open.
- Retracts on `remoteDelete` / `editMessage` upstream (see `signal-envelope`); the KB is not append-only-forever.

## Obsidian plugin reality in this vault family

- **Dataview is installed but NOT enabled** in CF. Do not write pages that depend on Dataview queries rendering.
- **Bases is enabled but there are zero `.base` files** — it is entirely unexercised. Author and test one `.base` view before betting the schema on it.
- Keep frontmatter **flat and portable**. Bases supports nested access (`property.subprop`), so nesting is not forbidden by the tool — flat is chosen for portability across both vaults and for legibility to members reading raw Markdown.
- Wikilinks, the graph view, and `> [!warning]` callouts for contested claims are all in active use in CF. Match them.

## Naming

`Research - <condensed question> - YYYY-MM`, mirroring CF's `Company - X`, `Investment - X in Y - YYYY-MM`, `Source - X - YYYY-MM` pattern. Keep titles stable — they are the wikilink target, and renaming breaks inbound links across both vaults.
