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
                                                       + fetch links the group
                                                         actually posted
                                                                    │
                                                (same firewall, inbound)
                                                                    ▼
                                              Markdown → private git repo
```

## One page per topic

The archive is not a log. A page is keyed on a stable `topic_key`, so later
research on a subject already covered **updates that page** — `last_verified`
advances, sources and open questions are unioned rather than replaced, and a
dated entry is appended under `## Updates`. Nothing is renamed, because the
filename is the wikilink target.

A page a human has edited is never touched: the bot stores a hash of the bytes
it last wrote, and if the file differs, the update is refused and recorded in
`Changelog/` for someone to reconcile by hand. `Dashboard/Research Overview.md`
is regenerated each run as a plain-Markdown index of every topic, so the archive
is navigable on a git host with no plugin and no Obsidian.

## Links posted in the chat

A link shared as evidence is a lead: the claim is about what the document says.
The deep stage can retrieve and read one, bounded by `SRB_FETCH_MAX_USES`
(`0` switches it off entirely).

The control is deterministic and lives in [`batch.py`](signal_research_bot/batch.py):
a URL is offered only if the transcript builder recorded it surviving redaction
into a line, **character for character**. The model echoes URLs through two
stages and is trusted with neither, so an invented, completed or tidied-up link
fails a set-membership test rather than being retrieved. Profile links are
redacted before that point and are additionally in the fetch tool's
`blocked_domains`.

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
from being committed. Runs in pre-commit *and* CI, because pre-commit alone is
bypassable.

It scans **this** repository automatically. The knowledge-base vault is a
separate repository and the hook does not reach it, so it has to be passed
explicitly:

```bash
python -m tools.scrub_check --all --vault /path/to/vault
```

That used to read "either repository", which was not true of anything the tool
did — and the first vault scan run after fixing it found a real over-claim in
the vault's own README.

Neither may be weakened to make a test pass. If you are contributing and a
check blocks you, the check is probably right.

## Also in this repository

[`tools/ai-graph-highlighter/`](tools/ai-graph-highlighter/) is a standalone
Obsidian plugin, unrelated to the bot. It is published from here for
convenience and is **not** covered by anything above: no network calls, no
Signal data, not exercised by the test suite, and outside the scope of
PRIVACY.md. Its own README says so at the top.

## Auditing this without reading all of it

| Question | Where to look |
|---|---|
| What can leave the machine? | [`signal_research_bot/egress.py`](signal_research_bot/egress.py) |
| Is that actually enforced? | [`tests/test_egress.py`](tests/test_egress.py) — each test is a sentence |
| Would you notice if it broke? | `python -m tools.mutation_test` — breaks the firewall on purpose and proves the tests catch it |
| What is stored, and is it encrypted? | [`cache.py`](signal_research_bot/cache.py); `looks_encrypted()` checks rather than assumes |
| What gets removed from a message? | [`redact.py`](signal_research_bot/redact.py) |
| Which links may be fetched, and who decides? | `task_urls` in [`batch.py`](signal_research_bot/batch.py) — set membership against the redacted transcript, not model output |
| What does a generated page look like? | [`.claude/skills/kb-schema/SKILL.md`](.claude/skills/kb-schema/SKILL.md) |
| What is sent to Claude, and how much does it cost? | [`.claude/skills/claude-cascade/SKILL.md`](.claude/skills/claude-cascade/SKILL.md) |

The `.claude/skills/` directory is published deliberately: it contains the
standing instructions the maintainers (human and AI) work under, so you can
read the rules, not just the result.

## Setup

Requires Docker. Nothing is installed on the host.

```bash
cp .env.example .env          # then fill it in; it is gitignored
docker compose build
```

**1. Link as a device on your Signal account.** One-time and interactive:

```bash
docker compose --profile link run --rm link
```

Scan the printed `sgnl://` URI from your phone. Choose **"Don't Transfer"** —
signal-cli cannot use the history archive, so accepting it wastes the one-time
offer.

**2. Find the group ID** and put it in `.env` as `SRB_GROUP_ID`:

```bash
docker compose exec signal-cli \
  signal-cli --config /data listGroups
```

**3. Fill in the roster.** Copy [`roster.example.json`](roster.example.json) to
`var/roster.json` (gitignored) and edit it. The file documents itself; the
short version is that this is the closed-world deny-list redaction treats as
its control, so its accuracy matters more than any other single input. Include
nicknames and common misspellings — a member who is not listed is a member
whose name can reach Anthropic.

Copying rather than editing in place is deliberate. A roster still holding the
example's `REPLACE-ME` values is *non-empty*, so it would pass every other
check while protecting nobody; that case is rejected on load rather than run.

**4. Start collecting.**

```bash
docker compose up -d
```

**5. Run a batch** (schedule this via Task Scheduler or cron):

```bash
docker compose --profile batch run --rm batch
```

Schedule **one** job. Two batches overlapping on the same topic would each read
the state the other is about to replace, and one update would be lost. Nothing
in the code enforces it.

### Migrating a vault written by an older version

Two one-shot commands, both with a `--dry-run` that writes nothing. `adopt`
gives pages from before the topic index a key; `repair` puts that key on the
page and recovers the tags, status and dates the index is missing, so those
pages can be recognised and linked to. Neither renames anything.

```bash
docker compose --profile batch run --rm --entrypoint python batch \
  -m signal_research_bot.kb.adopt --dry-run
docker compose --profile batch run --rm --entrypoint python batch \
  -m signal_research_bot.kb.repair --dry-run
```

`repair` changes exactly two lines per page and refuses any page whose bytes do
not match what the bot last wrote. Re-running it is a no-op, so an interrupted
run converges.

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
