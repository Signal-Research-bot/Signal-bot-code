---
name: privacy-invariants
description: Enforces this project's two non-negotiable privacy firewalls — the egress firewall (no group-member identity may reach the Anthropic API or the knowledge base) and the scrub firewall (no host or author identity may reach either git repo). Use this skill BEFORE editing egress.py, redact.py, identity.py, receiver.py, notify.py or anything under claude/, before adding any network call or dependency, before writing test fixtures, and before any commit or push. Also use it when asked to "loosen", "skip", "temporarily disable", or "work around" a redaction or scrub check.
---

# Privacy invariants

This project ingests other people's private group messages. Two firewalls carry the entire privacy claim. Both **fail closed**. Neither may be weakened to make a test pass, unblock a demo, or simplify code.

If a change would weaken either firewall, **stop and say so** rather than implementing it. Propose the alternative that preserves the invariant.

## Firewall 1 — egress (protects group members)

Everything crossing the network to Anthropic, and everything coming back before it touches the vault, passes `egress.py`.

**Must never leave this machine:**

- phone numbers in any format (E.164, national, spaced, hyphenated, with or without country code)
- Signal ACI / PNI UUIDs, any UUID shape
- member names, nicknames, profile names, `@mentions` — NFKC-normalized, case-insensitive, including possessives, diminutives, inflections and known misspellings
- the group name and the groupId
- email addresses, crypto addresses, links to member profiles or socials
- attachment filenames
- millisecond-precision timestamps (round to 15 minutes; ms timestamp + message length is a near-perfect join key against anyone else's copy of the chat)

**Must be true of every outbound request:**

- every speaker label is in the allowed pseudonym set (`Participant A`…), and nothing else
- endpoint is `/v1/messages`; no `mcp_servers`, no `file_id`, no batch wrapper unless explicitly the Phase 6 import path
- the assertion runs against the **serialized request body**, not the pre-serialization dict

On any violation: raise, quarantine the batch to `var/quarantine/`, alert. Never log the offending content — log the rule that fired and a hash.

**Run the same firewall over Claude's response** before rendering to the vault. Claude can echo back content it was given.

## Firewall 2 — scrub (protects the author and this machine)

Nothing about the operator or the host may reach either git repo. Both repos are read by other people; one is public.

**Categories** (the literal values live in `var/scrub_tokens.local`, which is gitignored — **never inline them into tracked files, including this skill**):

- the operator's real name, personal email, and work email
<!-- scrub-ok: absolute-windows-path -->
- the Windows username, and therefore any `C:\Users\<username>\...` path
- the machine hostname
- absolute Windows paths of any form (`[A-Za-z]:\`)
- the group name, member roster, any phone number or Signal UUID

**Structural rules that make this hold by construction, not vigilance:**

- zero hardcoded absolute paths anywhere; every path comes from an env var with a relative default
- the log formatter emits repo-relative paths only
- generated KB pages carry no `source_path`, hostname, or local run ID in frontmatter
- container names are static, never derived from `$COMPUTERNAME`
- test fixtures and recorded API responses are **synthetic only** — never captured from a real run

`tools/scrub_check.py` runs in pre-commit **and** CI. Pre-commit alone is insufficient because `SKIP=` bypasses it.

## Terminology

<!-- scrub-ok: privacy-word-overclaim -->
Say **"pseudonymised"**, never "anonymised" — in code, comments, commit messages, README, and PRIVACY.md.

<!-- scrub-ok: privacy-word-overclaim -->
Stable pseudonyms are reversible by whoever holds the key, and stylometry re-identifies authors regardless. Calling this "anonymisation" overstates it to exactly the non-expert audience the public repo is meant to reassure.

`scrub_check.py` enforces this with the `privacy-word-overclaim` rule. The only legitimate way past it is a `<!-- scrub-ok: privacy-word-overclaim -->` pragma on a line that is *explaining why the word is wrong* — as the two above do. Reaching for that pragma anywhere else means the wording should change instead.

## Things that look like fixes and are not

| Tempting | Why it's wrong |
|---|---|
| Hash phone numbers with a committed salt | ~10¹⁰ search space; brute-forced in seconds. HMAC the ACI UUID with a key from the OS keyring. |
| Use Presidio's `hash` operator for pseudonyms | Since v2.2.361 it defaults to a random salt, silently breaking pseudonym stability across runs. |
| Rely on NER to catch member names | PERSON F1 ~0.62–0.69. The closed-world roster deny-list is the control; NER is only a recall supplement. |
| Redact spans inside a sensitive message | For special-category content (health, politics, religion, sexuality) the sensitive fact is usually the whole message. Exclude at message level; bias to false positives. |
| Keep disappearing messages "since they're already cached" | Non-null `expiresInSeconds` means hard-drop. Persisting them contradicts the senders' explicit configuration. |
| Paste a real batch into the Console to debug a prompt | Console/Workbench usage is excluded from zero-retention. Synthetic fixtures only. |

## Before any commit

1. `python -m tools.scrub_check --all`
2. `git log --format='%an <%ae>' | sort -u` — no real name or mailbox
3. `git grep -nE '[A-Za-z]:\\\\'` — no absolute paths
4. Before the **first** push only: `gitleaks git` over full history (`.gitignore` is not retroactive)
