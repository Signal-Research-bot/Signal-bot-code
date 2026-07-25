# Security

## Reporting

Open a GitHub issue for anything that is not itself sensitive. For a finding
that would expose group members if published, contact the maintainer privately
first — the repository's contact address is on the organisation profile.

There is no bounty. There is a commitment to fix privacy defects before
anything else.

## What this system is protecting

Two distinct assets, with two distinct threat models.

**Group members' identities.** The adversary is anyone who obtains the
transcripts sent to Anthropic, the published knowledge base, or the public
source. Mitigation is [`egress.py`](signal_research_bot/egress.py), which fails
closed and validates the serialised request body.

**The operator's Signal account.** signal-cli runs as a *linked device*, so its
data directory holds keys that can send messages as the operator and read their
account state. Anyone who obtains that directory, or who can reach the
JSON-RPC port, has the account.

## Trust boundaries

| Boundary | Control |
|---|---|
| Container → Signal | signal-cli's own protocol; keys live in a named volume, never a bind mount into the repo tree |
| Worker → signal-cli | JSON-RPC on an internal Docker network. **The port is not published to the host.** Reaching it is equivalent to holding the account |
| Worker → Anthropic | `egress.py`. Nothing else in the codebase is permitted to make a network call |
| Repo → public | `tools/scrub_check.py` in pre-commit and CI |
| Archive → members | Read-only access via a GitHub organisation; revocation is not retroactive |

## Secrets, and where they live

| Secret | Location | If leaked |
|---|---|---|
| signal-cli data directory | Docker named volume | Full account compromise. Unlink from the phone immediately, then re-link |
| Pseudonym HMAC key | OS credential store, via `keyring` | Every participant can be re-identified from any stored transcript |
| Cache key (`SRB_CACHE_KEY`) | `.env`, gitignored | Cached raw messages readable |
| Anthropic API key | `.env`, gitignored | Billing abuse; revoke and rotate |

The pseudonym key is deliberately **not** in `.env`. An attacker with both the
cache and a key file sitting next to it can re-identify everyone; separating
them across two stores means one compromise is not automatically both.

## Known limitations

Stated plainly rather than buried.

- **Pseudonymisation is reversible by design.** See [PRIVACY.md](PRIVACY.md).
  Stylometric re-identification is not mitigated and cannot be.
- **Trust-and-safety flagging at Anthropic is undetectable to us.** Flagged
  content may be retained for up to two years and no signal comes back.
- **The quarantine directory holds unredacted content.** It exists so a false
  positive can be debugged without re-running against live data. It is
  gitignored and covered by the same retention policy as the cache.
- **The host is a personal Windows machine.** Disk encryption is the operator's
  responsibility. Note that a local-account Windows device can be "encrypted
  with a clear key" — it looks encrypted and is not. Verify with
  `manage-bde -status`, not by assumption.
- **NER-based redaction is a supplement, never the control.** General-purpose
  person-name recognition sits around 0.62–0.69 F1, which is not a privacy
  control. The roster deny-list is the control, and its accuracy is the single
  biggest determinant of redaction quality.
- **Revocation of archive access is not retroactive.** A departed member keeps
  whatever they already cloned.

## Supply chain

- signal-cli is pinned by **version and SHA-256**, verified at build time. An
  unpinned tag would let a rebuild silently change the code holding the
  account's identity keys.
- Python dependencies are pinned in `requirements.txt`.
- Images are **not published**. signal-cli is GPLv3 and distributing an image
  containing it carries obligations this project has not taken on.

## If a secret is committed

Order matters, and it is not the intuitive one:

1. **Rotate or revoke the secret first.** History rewriting is slow and
   incomplete; the exposure window is what matters.
2. Then rewrite history with `git-filter-repo --sensitive-data-removal` and
   force-push.
3. Accept that forks and existing clones still have it. GitHub cannot remove a
   commit from someone else's fork.

For the signal-cli data directory specifically, step 1 is: unlink the device
from the phone. That invalidates the keys immediately and is far more effective
than anything done to the repository.

## Verifying the controls yourself

```bash
python -m tools.scrub_check --all      # nothing identifying is committed
python -m pytest tests/test_egress.py  # the firewall's rules
python -m tools.mutation_test          # proves those tests would catch a regression
```

The last one matters most. A passing suite says the firewall works on the
inputs it was given; the mutation harness says the suite would go red if
someone weakened it. It caught a real hole during development — three tests
that appeared to prove evasion resistance were passing for an unrelated reason
and proving nothing.
