# signal-research-bot

Turns an ongoing Signal group conversation into a verified, sourced research
archive — without the group's identities ever reaching the AI service that does
the research.

Messages are collected continuously, pseudonymised and redacted locally, sent
to Claude in scheduled batches to extract and answer research questions, and
written into a private Obsidian vault that only group members can read.

**If you are a member of the group being processed, read
[PRIVACY.md](PRIVACY.md) first.** It is written for you and is more useful than
this file.

---

## How it works

```
Signal servers
   │  queue stays server-side while we are down
   ▼
signal-cli (native, no JRE)  ──JSON-RPC/TCP──►  receiver  ──►  encrypted cache
   --receive-mode=on-connection                                (SQLCipher)
                                                                    │
                                              scheduled window      ▼
                                                        pseudonymise + redact
                                                                    │
                                                         ┌──────────▼─────────┐
                                                         │  EGRESS FIREWALL   │ fail-closed
                                                         └──────────┬─────────┘
                                                                    ▼
                                    extract ──► triage+gate ──► cheap research
                                    (Sonnet)     (Sonnet)          (Haiku)
                                                                    │
                                                          resolved? ├─ yes ─► format
                                                                    │
                                                                    no
                                                                    ▼
                                                       deep research (Opus)
                                                                    │
                                                (same firewall, inbound)
                                                                    ▼
                                              Markdown → private git repo
```

## The two firewalls

Everything in this repository is arranged around two checks that fail closed.

**[`egress.py`](signal_research_bot/egress.py) — protects the group.** The only
module permitted to talk to the network. It validates the *serialised* request
body, so nothing can hide in a nested field: there is a test proving a member's
name planted in a tool description is caught. Three assertions — no known
identity, nothing shaped like identity, no speaker label we did not allocate —
plus a structural check on the request itself. Every model response passes the
same checks before it can reach the archive.

**[`tools/scrub_check.py`](tools/scrub_check.py) — protects the operator.**
Blocks the operator's name, emails, username, hostname and any absolute path
from reaching either repository. Runs in pre-commit *and* CI, because
pre-commit alone is bypassable.

Neither may be weakened to make a test pass. If you are contributing and a
check blocks you, the check is probably right.

## Auditing this without reading all of it

| Question | Where to look |
|---|---|
| What can leave the machine? | [`signal_research_bot/egress.py`](signal_research_bot/egress.py) |
| Is that actually enforced? | [`tests/test_egress.py`](tests/test_egress.py) — each test is a sentence |
| Would you notice if it broke? | `python -m tools.mutation_test` — breaks the firewall on purpose and proves the tests catch it |
| What is stored, and is it encrypted? | [`cache.py`](signal_research_bot/cache.py); `looks_encrypted()` checks rather than assumes |
| What gets removed from a message? | [`redact.py`](signal_research_bot/redact.py) |
| What is sent to Claude, and how much does it cost? | [`.claude/skills/claude-cascade/SKILL.md`](.claude/skills/claude-cascade/SKILL.md) |

The `.claude/skills/` directory is published deliberately: it contains the
standing instructions the maintainers (human and AI) work under, so you can
read the rules, not just the result.

## Setup

Requires Docker. Nothing is installed on the host.

```bash
cp .env.example .env          # then fill it in; it is gitignored
docker compose -f docker/docker-compose.yml build
```

**1. Link as a device on your Signal account.** One-time and interactive:

```bash
docker compose -f docker/docker-compose.yml --profile link run --rm link
```

Scan the printed `sgnl://` URI from your phone. Choose **"Don't Transfer"** —
signal-cli cannot use the history archive, so accepting it wastes the one-time
offer.

**2. Find the group ID** and put it in `.env` as `SRB_GROUP_ID`:

```bash
docker compose -f docker/docker-compose.yml exec signal-cli \
  signal-cli --config /data listGroups
```

**3. Fill in the roster** at `var/roster.json` (gitignored). This is the
closed-world deny-list that redaction treats as its control — its accuracy
matters more than any other single input. Include nicknames, common
misspellings, and every phone number format people actually use.

**4. Start collecting.**

```bash
docker compose -f docker/docker-compose.yml up -d
```

**5. Run a batch** (schedule this via Task Scheduler or cron):

```bash
docker compose -f docker/docker-compose.yml --profile batch run --rm batch
```

## Before you turn it on

- **Tell the group.** This is not a courtesy. It is what makes the project
  defensible under Signal's terms and Anthropic's usage policy, both of which
  frame undisclosed collection of other people's messages as prohibited.
- **Decide about disappearing messages.** If the group has them on, this
  software drops those messages entirely and always will.
- **Offer an opt-out** and be able to honour it.

## Operational notes

- **The receiver must stay connected.** `--receive-mode=on-connection` means
  Signal queues for us while we are down. Do not change it, and do not swap in
  `signal-cli-rest-api`: its fan-out drops already-acknowledged messages when
  the consumer is slow, silently and permanently.
- **The linked device expires.** Roughly 45 days of receiver inactivity, or ~30
  days with the phone offline, unlinks it. Re-linking needs a human with the
  phone; it cannot be automated. `last_seen` is recorded so you can alarm on it.
- **Docker Desktop starts on user sign-in.** An unattended reboot with nobody
  logged in means nothing runs, with no error. Decide whether you accept that
  or enable auto-login.
- **Never publish the images.** signal-cli is GPLv3; running it is fine,
  distributing an image containing it carries obligations this project has not
  taken on.

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m pytest              # full suite
.venv/Scripts/python -m tools.mutation_test # prove the egress tests have teeth
.venv/Scripts/python -m tools.scrub_check --all
```

Test fixtures are synthetic and assemble phone numbers, emails and UUIDs at
runtime rather than writing them as literals, so no tracked file ever contains
a string that looks like real identity.

## Licence

MIT — see [LICENSE](LICENSE). Chosen so group members can freely fork and audit
without obligations. If you would prefer copyleft, it is a one-file change.
