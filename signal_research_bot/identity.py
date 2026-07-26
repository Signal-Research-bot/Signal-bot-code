"""Pseudonym allocation and the roster. The ONLY module holding raw identity.

Everything downstream sees `Participant A`. The mapping from a Signal ACI to
that label lives here, on disk, encrypted-at-rest by the host and keyed by a
secret held in the OS credential store. It never crosses the network.

Two things this module is careful about, both from
.claude/skills/privacy-invariants:

* **HMAC the ACI UUID, never the phone number.** A phone number has ~10^10 of
  search space. Hashing one with a salt that lives in the repo -- or that an
  attacker can guess -- is reversible by brute force in seconds. The ACI is a
  random 128-bit value, so an HMAC of it is not.
* **The key never touches the repo.** It is generated on first use and stored
  in the OS credential store (Windows Credential Manager via `keyring`). The
  only supported alternative is an environment variable, for CI.

Keeping the mapping is deliberate, not an oversight: it is the only mechanism
by which a member's erasure or access request can actually be honoured.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Iterable

SERVICE = "signal-research-bot"
KEY_NAME = "pseudonym-key"
KEY_ENV = "SRB_PSEUDONYM_KEY"          # hex; CI and tests only
KEY_FILE_ENV = "SRB_PSEUDONYM_KEY_FILE"  # path; the container path (see below)
INTERNAL_ID_CHARS = 12                 # 48 bits of the HMAC: ample, and short
# Sentinel in roster.example.json. Kept in sync with that file by
# test_roster_template_is_rejected_until_edited.
PLACEHOLDER_MARKER = "REPLACE-ME"


class KeyUnavailable(RuntimeError):
    """No pseudonym key could be loaded or created.

    Fails closed. Running without a stable key would silently produce fresh
    pseudonyms every run, which breaks cross-window deduplication and quietly
    destroys the ability to honour an erasure request.
    """


def _keyring():
    try:
        import keyring  # noqa: PLC0415 -- optional at import time, required at use
    except ImportError as exc:  # pragma: no cover - depends on host
        raise KeyUnavailable(
            "keyring is not installed and SRB_PSEUDONYM_KEY is unset. "
            "Install keyring, or set the env var for a non-interactive run."
        ) from exc
    return keyring


def _parse_key(raw: str, source: str) -> bytes:
    try:
        key = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise KeyUnavailable(f"{source} is not valid hex") from exc
    if len(key) < 32:
        raise KeyUnavailable(f"{source} must be at least 32 bytes of hex")
    return key


def load_or_create_key(*, allow_create: bool = True) -> bytes:
    """Return the 32-byte pseudonym key, creating it on first use.

    Resolution order: SRB_PSEUDONYM_KEY, then a key file, then the OS
    credential store.

    The credential store is the strongest option and the right one when the
    pipeline runs on the host. It does NOT survive containerisation: keyring
    has no usable backend inside a Linux container, so the batch job would
    raise NoKeyringError and never run. An audit caught this before the first
    live run.

    The key file is therefore the supported container path. It is deliberately
    a SEPARATE file from .env: the point of the split is that an attacker who
    obtains the message cache does not thereby obtain the means to undo the
    pseudonyms, and putting both in one file would defeat exactly that. It is
    weaker than the credential store -- a file is a file -- and that trade is
    stated here rather than hidden.
    """
    env = os.environ.get(KEY_ENV)
    if env:
        return _parse_key(env, KEY_ENV)

    key_file = os.environ.get(KEY_FILE_ENV, "").strip()
    if key_file:
        path = Path(key_file)
        if path.exists():
            return _parse_key(path.read_text(encoding="utf-8"), path.name)
        if not allow_create:
            raise KeyUnavailable(f"{path.name} does not exist and creation is disabled")
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key.hex(), encoding="utf-8")
        try:
            path.chmod(0o600)          # no-op on Windows; meaningful in the container
        except OSError:
            pass
        return key

    kr = _keyring()
    stored = kr.get_password(SERVICE, KEY_NAME)
    if stored:
        return bytes.fromhex(stored)

    if not allow_create:
        raise KeyUnavailable("no pseudonym key stored and creation is disabled")

    key = secrets.token_bytes(32)
    kr.set_password(SERVICE, KEY_NAME, key.hex())
    return key


def internal_id(key: bytes, aci: str) -> str:
    """Stable, non-reversible handle for one participant.

    HMAC rather than a bare hash so the key -- not merely the algorithm --
    is what protects it.
    """
    if not aci:
        raise ValueError("refusing to derive an id from an empty ACI")
    digest = hmac.new(key, aci.strip().lower().encode("utf-8"), sha256).hexdigest()
    return digest[:INTERNAL_ID_CHARS]


def _label_for_index(i: int) -> str:
    """0 -> 'Participant A' ... 25 -> 'Participant Z', 26 -> 'Participant AA'."""
    letters = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return f"Participant {letters}"


@dataclass
class Roster:
    """Known identity for the target group. Loaded from a gitignored file.

    This is the closed-world deny-list that `redact.py` treats as its control.
    Its accuracy is the single biggest determinant of redaction quality --
    NER cannot make up for a name that is missing here.
    """

    names: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    opted_out: frozenset[str] = frozenset()   # ACIs whose messages are dropped
    group_name: str | None = None

    @classmethod
    def load(cls, path: Path) -> "Roster":
        if not path.exists():
            raise FileNotFoundError(
                f"roster not found at {path.name}. Redaction cannot run without "
                f"it: an empty deny-list silently redacts nothing."
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        roster = cls(
            names=tuple(raw.get("names") or ()),
            phones=tuple(raw.get("phones") or ()),
            opted_out=frozenset(raw.get("opted_out") or ()),
            group_name=raw.get("group_name"),
        )
        roster._reject_placeholders()
        return roster

    def _reject_placeholders(self) -> None:
        """Refuse a roster still carrying template text.

        The empty-roster check upstream is not enough. A roster copied from the
        template and not edited is *non-empty*, so it passes every other check
        and the pipeline runs at full speed while the deny-list protects nobody
        -- which is indistinguishable from working until a real name turns up
        in an outbound payload. Failing here makes that mistake loud.
        """
        values = [*self.names, *self.phones, self.group_name or ""]
        for value in values:
            if PLACEHOLDER_MARKER in value.upper():
                raise KeyUnavailable(
                    "the roster still contains template placeholder text "
                    f"({PLACEHOLDER_MARKER!r}). Fill in var/roster.json with the "
                    "group's real names, numbers and group name before running: "
                    "an unedited roster redacts nothing while reporting success."
                )

    def name_variants(self) -> set[str]:
        """Every surface form of a roster name that must be caught.

        Presidio's deny_list is case-sensitive, exact-token, and has no notion
        of possessives, so the variants have to be enumerated here rather than
        assumed. This is a floor, not a ceiling: nicknames and misspellings
        belong in the roster file itself.
        """
        out: set[str] = set()
        for name in self.names:
            n = name.strip()
            if not n:
                continue
            for part in {n, *n.split()}:
                if len(part) < 2:
                    continue      # initials generate too many false positives
                out |= {part, f"{part}'s", f"{part}s'", f"{part}’s"}
        return out


@dataclass
class PseudonymStore:
    """Maps ACIs to stable labels, persisting the assignment.

    Labels are assigned in first-seen order and never reused, so `Participant A`
    means the same person in every window and every KB entry.
    """

    key: bytes
    path: Path
    _by_id: dict[str, str] = field(default_factory=dict)   # internal_id -> label

    def __post_init__(self) -> None:
        if self.path.exists():
            self._by_id = json.loads(self.path.read_text(encoding="utf-8"))

    def label(self, aci: str) -> str:
        iid = internal_id(self.key, aci)
        if iid not in self._by_id:
            self._by_id[iid] = _label_for_index(len(self._by_id))
            self._flush()
        return self._by_id[iid]

    def known_labels(self) -> set[str]:
        return set(self._by_id.values())

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._by_id, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)      # atomic: never a half-written mapping


def allowed_labels(count: int) -> set[str]:
    """The full set of labels the egress firewall will accept."""
    return {_label_for_index(i) for i in range(count)}


def is_opted_out(roster: Roster, aci: str | None) -> bool:
    return bool(aci) and aci in roster.opted_out


def redact_participants(labels: Iterable[str]) -> str:
    """Human-readable participant summary for a batch header."""
    return ", ".join(sorted(set(labels)))
