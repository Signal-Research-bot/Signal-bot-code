---
name: graph-architect
description: Decomposes a complex task into an explicit graph of bounded nodes, real data-dependency edges, parallel groups, barriers and verifier gates before any work begins. Use this skill when a task has genuinely independent parallel parts, multiple specialised roles, branching logic, or needs a verification gate before results are trusted — for example broad research sweeps, multi-file audits, or fan-out reviews. Skip it entirely for simple, linear, exploratory or one-shot work.
---

# Graph architect

Adapted from the operator's `graph_architect` skill in the CF vault. Applies to planning fan-out work; it is a thinking discipline, not a code pattern.

## When this earns its keep

Apply it only when the task has **independent parallel parts, multiple specialised roles, branching logic, or a need for verification gates**. Otherwise treat the work as a normal single loop and just do it. Most tasks are the second kind — reaching for a graph on a linear task adds ceremony and latency without buying anything.

## The discipline

**1. Decompose into the minimal set of nodes.** Each node is one bounded job with one clear input and one validated output. Name it and state its single responsibility. If you cannot state the responsibility in a sentence, it is two nodes.

**2. Identify only real edges.** An edge exists **only** when node B's input requires node A's output. For every sequential "then", ask: *does data actually flow?* If not, delete the edge and mark the pair parallel. Flag hidden edges — shared files, shared APIs, locks, a single rate limit.

**3. Draw the topology.** Prefer diamonds: fan-out → parallel workers → barrier → merge/verify. Declare parallel groups, join barriers, conditional routers, and any controlled cycle (with a budget and a dry-out rule). Define the shared state object and who may write to each field. Place verifier or human-gate nodes on high-stakes edges. State the stop rule and the failure handling.

**4. Justify the shape.** Say in one line why this is not a linear chain. If you cannot, it probably should be one.

## Barriers are expensive — default to pipelining

A barrier waits for every worker before anything proceeds; the slowest item sets the pace for all. Use one **only** when the next stage genuinely needs cross-item context:

- deduplicating or merging across the full result set before expensive downstream work
- early-exit on a global count ("zero findings → skip verification entirely")
- a prompt that explicitly compares one item against all the others

A barrier is **not** justified by "I need to flatten/map/filter first" (do that inside a stage), by "these stages are conceptually separate" (separate ≠ synchronised), or by "it reads more cleanly".

## Verification is a node, not an afterthought

For anything where a plausible-but-wrong answer is costly, add an adversarial verifier node whose instruction is to **refute**, not to confirm — defaulting to "refuted" when uncertain. Where a finding can fail in more than one way, give each verifier a distinct lens (correctness, security, does-it-reproduce) rather than running N identical skeptics; diversity catches failure modes that redundancy cannot.

For unknown-size discovery, loop until K consecutive rounds surface nothing new, deduplicating against everything seen — **not** against what survived verification, or rejected findings resurface every round and the loop never converges.

## In this project specifically

The `Workflow` tool already implements this execution model — `pipeline()` for the default no-barrier case, `parallel()` when a barrier is genuinely required, schemas for validated node outputs. Use this skill to decide the **shape**; use `Workflow` to run it.

Two standing rules here: agents doing research must be told they are **read-only** when the repo is in a state where stray writes would be damaging, and any fan-out that touches message content inherits the constraints in `privacy-invariants` — a subagent is not an exemption from the egress firewall.

If a graph would bound coverage (top-N, no-retry, sampling), **log what was dropped**. Silent truncation reads as "covered everything" when it did not.
