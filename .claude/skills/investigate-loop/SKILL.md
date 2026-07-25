---
name: investigate-loop
description: Runs a rigorous goal-based investigative loop — assess, plan, act, evaluate, update — until a claim meets an explicit evidence bar built on primary sources, triangulation, and the separation of fact from inference from speculation. Use this skill when writing or revising the stage-3 research prompt, when manually investigating a contested knowledge-base entry, and for any open-ended question about ownership, financial anomalies, regulatory action, or an allegation where the path to an answer is not yet known.
---

# Investigative loop

Ported from the operator's `investigate-loop` skill in the CF vault, and adapted to serve two roles here:

1. **The contract stage 3 must implement.** The deep-research prompt is this loop, bounded by `max_uses`.
2. **The procedure to follow manually** when a KB entry is contested and needs a human-driven pass.

The point of porting it is that the bot and the operator are held to the *same* standard, so a generated record and a hand-written one are comparable evidence.

## The bar

A claim is finished only when **all** of these hold:

- key claims tested against **primary sources** — filings, court records, regulatory documents, financial statements, contracts, transcripts, first-party statements
- evidence triangulated across genuinely independent sources wherever possible
- fact, well-supported inference, allegation, and speculation **explicitly separated**
- material gaps, alternative explanations, and remaining unknowns listed
- sources cited with specific identifiers — document name, date, page, filing number
- the output could withstand editorial and legal scrutiny

## Loop protocol

1. **Assess** — current evidence against the bar. What is verified? What is contested or missing?
2. **Plan** — the single highest-value next action: the one document, database, or cross-reference that most reduces uncertainty.
3. **Act** — execute it. Prefer primary sources. Record exact findings.
4. **Evaluate** — does this move any claim toward the bar? Does it open or close lines of inquiry?
5. **Update** — maintain a short running status: verified so far / active hypotheses / still missing / dead ends.
6. Repeat until the bar is met or a stop condition fires.

**Stop conditions:** the bar is met; the search budget is exhausted (`max_uses` — 3 at stage 2.5, 5 at stage 3); or you are hard-blocked. If blocked, state the precise blocker and stop. Do not pad an answer to fill the budget, and do not keep searching once the bar is met.

## Mapping the bar onto this project's schema

| Loop concept | Where it lands |
|---|---|
| Verified against primary sources | `confidence: primary` |
| Triangulated across independent sources | `confidence: corroborated` |
| One source only, uncontradicted | `confidence: single-source` |
| Could not be established | `confidence: unverified`, `research_status: open` |
| Sources conflict irreconcilably | `research_status: contested` + a `> [!warning]` callout |
| Inferences, clearly labelled | `## Answer`, marked as inference in prose — never in `## Evidence` |
| Alternative explanations, gaps | `## Contradictions`, `## Open questions` (both **required**) |
| Specific citations | `## Evidence` table: source, verbatim quote, confidence |

`## Evidence` carries only what a source actually says. Inference belongs in `## Answer` and must be visibly labelled as inference. This separation is the whole point of the bar — a record that blurs it is worse than no record, because it launders a guess into the knowledge base.

## Standards that do not bend

- Never present inference or speculation as fact.
- Prefer primary sources over secondary reporting. When using secondary reporting, assess the outlet's track record, conflicts, and original sourcing.
- **Seek disconfirming evidence, not only confirming.** Test the alternative explanation explicitly.
- Flag conflicts of interest, incomplete records, and access limitations.
- Treat unproven allegations against private individuals with caution and context.
- Do not invent documents, quotes, or data. Every URL in a generated record must come from the harvested search allowlist (see `claude-cascade`); a plausible-looking citation that was never retrieved is the failure mode this rule exists to prevent.

## A note specific to this pipeline

Stage 3 runs on pseudonymised chat. The questions arriving from the group will often be **underspecified or partisan** — someone asserting a thing confidently is not evidence that the thing is contested, and it is not evidence that it is true. Restate the question neutrally before investigating it, and record the restatement. If the question cannot be made answerable, `research_status: dropped` with a one-line reason is a legitimate and cheap outcome — much better than an authoritative-sounding answer to a question nobody actually asked.
