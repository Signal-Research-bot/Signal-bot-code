---
name: kb-schema
description: Defines the Obsidian knowledge-base contract for generated research records — the frontmatter vocabulary, the research_status versus status collision with the operator's existing vault, the body section layout, and the rules about which files the bot may create or touch. Use this skill before editing anything under kb/, before changing frontmatter keys or values, before adding a Bases or Dataview view, and whenever a generated page needs a new field.
---

# Knowledge-base schema

Output is Markdown in the vault at `$SRB_KB_DIR` — a git repo and an Obsidian vault. **That vault is normally one a person also writes in by hand**, and everything below is shaped by it.

This file used to say the vault must be kept separate from the operator's own, and that the bot must never write into it. That separation was reversed deliberately, because it caused the thing it was meant to prevent: a bot that cannot see the operator's archive cannot deduplicate against it. Triage was shown five pages out of 277 and re-researched, from primary sources at Opus prices, five subjects the vault already covered — including an investment whose amount, valuation, date and press release were already on a page marked `confidence: primary`.

What replaced it is stronger than what it replaced, because it does not depend on which vault is which:

- **The bot owns three directories and cannot construct a writer for a fourth.** `Research Log/`, `Changelog/`, `Dashboard/` — `writer.OWNED_SUBDIRS`, checked at construction, so there is no window in which a writer exists that could reach a hand-written page.
- **Commits are staged by pathspec over those three, never `git add -A`**, and the staged set is verified before the commit. The operator's unfinished work, a plugin's `node_modules` and their editor state are not the bot's to commit.
- **`$SRB_FOREIGN_VAULT_DIR` still exists** and still means "a vault this must never be pointed at" — now naming a genuinely unrelated one. On the host it is compared; in the container nothing but the one bind mount is reachable, which is the real containment there.

Never write outside the three owned directories, and never assume a page you did not write is yours to touch.

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

### `confidence` carries only the two positive grades

Measured across the live vault: `confidence:` appears on 88 hand-written pages and has only ever held `primary` (48) and `corroborated` (40). The weaker grades are there too — as **tags**: `unverified` on 54 pages, `single-source` on 35, on pages carrying no `confidence:` key at all. That is the convention, not an omission. The key asserts a positive evidence grade tied to source type; its absence plus a tag says the evidence did not reach one.

So `render.frontmatter` emits `confidence:` **only** for `primary` and `corroborated`, and puts a weaker grade in `tags` instead. This file previously called adding the weak values to the key "an intentional widening", which was wrong in a way that mattered: writing four values into a key that has held two makes the field ambiguous across the whole corpus with no way to tell afterwards which scale any page used — and the vault's Dashboard has a Confidence Breakdown table reading exactly it.

Nothing is lost. All four grades live in the sidecar, where `_CONFIDENCE_ORDER` still ranks them for the stronger-value-wins merge. The divergence is at the render boundary only.

### `status:` is not ours, ever

`status:` holds a deal-lifecycle vocabulary on 88 pages — `completed` (20), `closed` (16), `n/a` (11), `announced` (6), plus twenty free-text one-offs like `exited 2026-03-24 at $10.80/share`. Research state in that key corrupts every page and every Deal Terms table that reads it. `frontmatter()` asserts the line cannot be produced.

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

- **Creates and updates its own pages**, under `Research Log/`, `Changelog/` and `Dashboard/` — and *only* those, enforced at writer construction rather than by convention. (`Sources and References/` was in this list and the bot never wrote to it; in the live vault it holds 32 hand-written source pages, which is exactly why the bot must not.) One file per **topic**, not per run: a page is a living document keyed on `topic_key`, and later research on the same subject updates it in place. This said "Creates only. One file per record" until that rule was found to produce unlinked near-duplicates whenever the model phrased a title differently, and to silently discard the research whenever it phrased one the same.
- **Never edits a human-authored page.** Never edits a page it did not create. The bot stores a hash of the bytes it last wrote; if the file on disk differs, a person has been in it — the update is refused, logged, and recorded in the changelog for the operator to reconcile by hand. More load-bearing than ever now that the vault is mostly hand-written — but note honestly what it covers: the `content_sha` guard needs a sidecar, and hand-written pages have none, so for them the protection is the directory allowlist plus `write(overwrite=False)`, not the hash.
- **Never adopts a directory it does not own.** `kb.adopt` writes sidecars with `managed=False`, which routes a topic to the append-only path — and that path calls `write(..., overwrite=True)`. Pointed at a directory of hand-written pages it would hand the bot permission to bump their dates and append sections to all of them. Run it against `Research Log/` and nothing else.
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

**`Research - <condensed subject>`**, matching the `Type - Subject[ - Date]` grammar every page in the vault already follows: `Company - X`, `Investment - X in Y - YYYY-MM`, `Person - X`, `Source - X - YYYY-MM-DD`, `Connection - X to Y`, `Analysis - X`.

Applied in code (`render.research_title`), not asked of the model, and it is doing two jobs. The obvious one is that a page not matching the grammar reads as an intruder and sorts away from its own kind. The load-bearing one is that it is a **namespace guard**: a model that titles a page `Company - Anchorage Digital` would otherwise aim at the filename of a page a human wrote. The writer refuses and reports a collision, which is safe — but it is a research task lost to a naming accident.

A date is deliberately *not* appended. A date in a filename says when the page was opened, and a page is a living document whose newest content may be months later; `first_raised` and `last_verified` carry that honestly. `Research - <question> - YYYY-MM` survives as the **no-title fallback only** (`title_for`), rare enough to be a signal.

`slug()` **substitutes** path separators rather than deleting them. Deleting produced `TreasuryFed`, `BlackRockAnchorage` and `ZaguryElektronPeak` on three of the first five pages, from `Treasury/Fed`, `BlackRock/Anchorage` and `Zagury/Elektron/Peak`. A filename is the permanent wikilink target, so a mangled one is mangled forever.

## Every page ends with `## Related Pages`

271 of the vault's 272 hand-written pages close with a bulleted list of wikilinks, including back to the hub pages. That convention is why the vault has two orphans; a generated page that skips it is an orphan by construction — nothing links to it and it links to nothing outside its own tag cluster.

The hub names come from `$SRB_VAULT_HUBS` (`render(..., hubs=)`), not from this module, because they are a property of one vault. The same links go in `related:` frontmatter, because the vault carries both and two lists that disagree is worse than either.

Keep titles stable — they are the wikilink target, and renaming breaks inbound links across both vaults.

`topic_key`, not the title, is what joins later research to an existing page. So a page keeps the filename it was created with even when the same subject is raised again and phrased differently — the key matches, the title is discarded, and the link target survives.
