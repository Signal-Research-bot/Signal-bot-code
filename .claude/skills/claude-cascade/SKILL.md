---
name: claude-cascade
description: Defines this project's five-stage Claude cost cascade — which model and parameters each stage uses, the hard gate that keeps most tasks away from Opus, the escalation criteria from the cheap research pass to the deep one, and the grounding and error-handling rules that are not optional. Use this skill before editing anything under claude/, before changing a model ID, effort level, or max_uses, before adding a pipeline stage, and whenever asked to reduce API cost or improve answer quality.
---

# The Claude cascade

Opus is the last resort, not the default. Every earlier stage exists so Opus only sees questions that are both worth answering and genuinely hard.

For API mechanics — model IDs, parameter shapes, streaming, caching, structured outputs — **use the bundled `claude-api` skill; it is authoritative.** This skill covers only the decisions specific to this pipeline.

## Stages

| Stage | Model | Key params |
|---|---|---|
| 1 — extract candidates | `claude-sonnet-5` | `thinking:{type:"disabled"}`, `output_config:{effort:"low", format:{…}}` |
| 2 — triage, dedupe, score | `claude-sonnet-5` | `effort:"medium"`; KB digest as a `cache_control:{type:"ephemeral",ttl:"1h"}` prefix |
| 2.5 — cheap research | `claude-haiku-4-5` | `web_search` with `max_uses:3`; `output_config.format` with required `resolved: bool` |
| 3 — deep research | `claude-opus-5` | `thinking:{type:"adaptive"}`, `effort:"high"`, `web_search` `max_uses:5`, **no `output_config`**, `fallbacks:"default"` |
| 3b — to KB record | `claude-sonnet-5` | `effort:"low"`, no tools, `output_config.format` |

The stage-2 cached digest **must exceed 1024 tokens** or it silently will not cache — that is Sonnet 5's minimum cacheable prefix (512 is Opus 5 only). A digest that quietly falls under the threshold looks identical to one that works, except for the bill.

Stages 3 and 3b are split defensively: citations and structured outputs have a documented 400 conflict, scoped to user-provided document blocks and undocumented for web-search citations. Smoke-test before collapsing them. The split independently buys per-stage retry granularity and lets 2.5 and 3 share one formatter.

**Do not downgrade stage 1 to Haiku.** It saves under $1/month and costs real accuracy on messy multi-party chat.

## The gate — this is where the money is

Three filters between stage 2 and anything expensive, all enforced **in code**, never by prompting:

- `worth >= threshold` — starts strict. Banter, rhetorical questions, and anything already answered in the KB never reach a research stage. A subject the archive covers is revisited **only** when triage reports genuine `new_information`: a development, a contradiction, or a source the entry does not cite. A restatement is still dropped for free.
- `max_tasks_per_window` — a hard integer cap. Take the top *N* by `worth`; log the remainder as deferred and surface them in the run summary. **Never truncate silently** — a silently dropped task reads as "nothing was missed".
- `max_updates_per_window` — of that cap, how much may go on revisiting. An update clears the same threshold and the same cap as a fresh lead, and at equal worth a new subject outranks it, so "new information" cannot become an unbounded spend channel.

Dropping a task costs nothing. Tightening the gate by one task per day saves more than the entire cheap-pass mechanism.

## Escalation, 2.5 → 3

Escalate when the cheap pass returns `resolved: false`, or any of:

- sources disagree, or the only sources are secondary or single-source
- the answer needs synthesis across more than ~3 sources
- it touches a claim already flagged contested in the KB
- stage 2 marked `difficulty: high`, regardless of what 2.5 concluded

Prompt stage 2.5 to **prefer escalating**: *"if you are not confident this is settled by primary sources, set `resolved: false`."* Haiku is materially weaker at adversarial source evaluation — the exact skill this project's research standard depends on — so a confident-but-wrong cheap answer is the failure mode to design against, not cost.

**Stage 2.5 is on probation.** An escalated task pays twice. At a 50% resolve rate the cascade saves ~25–30%; below ~30% it is break-even and stage 2.5 should be switched off. Log the `resolved` rate from run one and check it against actual spend after ~2 weeks. Web search bills at a flat $10/1,000 regardless of model, so the saving is on tokens only — and escalation adds searches.

## Grounding

- Harvest ground-truth URLs from the `web_search_tool_result` blocks in stages 2.5 and 3.
- In 3b, **reject any `sources[].url` outside that harvested allowlist at parse time**, and require a verbatim `quote` per source.
- Make `contradictions` and `open_questions` **required** schema fields — an empty list must be a deliberate assertion, not an omission.
- Do **not** set `response_inclusion:"excluded"`. It drops exactly the blocks the allowlist is harvested from; it and grounding are in direct conflict.
- Tag confidence as `primary` / `corroborated` / `single-source` / `unverified`, matching the operator's existing research standard.

## Error handling that is not optional

- `stop_reason:"refusal"` returns **HTTP 200 with a possibly empty `content` array**. Check `stop_reason` before touching `content[0]`. `stop_details` is populated only on refusal — guard it.
- Empty search results mean *no results*, not an error. Mark `confidence: unverified`; do not retry.
- Web search must be enabled org-wide in the Console. Request-level `allowed_domains` must be a **subset** of any org-level allowlist or the call 400s; `allowed_domains` and `blocked_domains` are mutually exclusive.
- Every KB write is keyed on the record's `topic_key`, so a crashed run rewrites the same page rather than opening a second one beside it, and the writer reports whether it created, updated, left the page unchanged or refused. The batch counts only what it actually wrote. This bullet used to say "keyed on a task hash", which was never true of anything the code did: the key was the model-authored title, and two records condensing to one title meant the second was silently discarded and announced as written.
- Batch API rejects `fallbacks`, `stream:true`, and `speed`. A batched stage 3 cannot auto-recover from a refusal — re-queue refused tasks synchronously.

## Cost discipline

Write `metrics.jsonl` every run: candidates extracted, survivors after the gate, tasks deferred by the cap, stage-2.5 `resolved` rate, escalations, and `usage` + search count per stage. Set a Console spend cap as a backstop.

Search results bill as input tokens on **every iteration** of the search loop, not once, and Opus 5 thinks by default at `effort:"high"`. Those two facts are why a research task costs ~$0.65 rather than the ~$0.16 a naive estimate suggests.
