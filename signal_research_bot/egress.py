"""The egress firewall. The only module permitted to talk to the network.

If you read one file in this repository to decide whether to trust it, read
this one. Everything the pipeline sends to Anthropic passes `check_outbound`,
and everything Anthropic sends back passes `check_inbound` before it can reach
the knowledge base. Both fail closed.

The checks run against the **serialized** request body, not the dict that
produced it. A dict walk has to know where to look; serialization flattens
every nested structure, tool definition, system prompt and message into one
string, so nothing can hide in a field nobody thought to inspect.

Design notes worth knowing before changing anything here:

* Redaction is *upstream* of this module and is expected to have done its job.
  This is the backstop that assumes redaction has a bug. That is why it
  re-checks classes `redact.py` already handles -- defence in depth is the
  entire point, and a duplicated check costs microseconds.
* Violations never log the offending text. They carry a rule name and a
  truncated SHA-256 of the payload, which is enough to correlate a quarantine
  file with a log line without putting message content into a log.
* Text is normalised before matching: NFKC, path separators folded, and
  zero-width characters stripped. A zero-width space inside a name is a real
  and trivial way to defeat a naive substring check.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from .identity import Roster

# Characters that render as nothing but break a naive substring match.
_INVISIBLE = dict.fromkeys(
    map(ord, "​‌‍⁠﻿­͏"), None
)

SPEAKER_RE = re.compile(r"\bParticipant\s+([A-Z]{1,3})\b")
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+(?![\w.-])")
UUID_RE = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])",
    re.I,
)
E164_RE = re.compile(r"(?<![\w+])\+[1-9]\d{7,14}(?![\w])")
SEPARATED_PHONE_RE = re.compile(
    r"(?<![\w])(?:\+\d[\d\s().-]{6,17}\d|0\d[\d\s().-]{6,15}\d"
    r"|\d{2,4}[\s().-]\d{2,4}[\s().-]\d{2,6})(?![\w])"
)

# Request-shape keys that would route data somewhere this module cannot vet.
FORBIDDEN_KEYS = frozenset(
    {"mcp_servers", "file_id", "container", "container_upload", "requests"}
)


class EgressViolation(Exception):
    """A payload failed the firewall. Carries no message content, by design."""

    def __init__(self, rule: str, detail: str, payload_sha: str) -> None:
        super().__init__(f"egress blocked [{rule}]: {detail} (sha={payload_sha})")
        self.rule = rule
        self.detail = detail
        self.payload_sha = payload_sha


def _normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    return folded.replace("\\", "/")


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()[:12]


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


@dataclass(frozen=True)
class Policy:
    """Everything the firewall needs to know about what must not escape."""

    roster: Roster
    allowed_labels: frozenset[str]
    group_id: str | None = None
    # Derived once; matching happens on every outbound call.
    _name_variants: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def build(
        cls, roster: Roster, allowed_labels: Iterable[str], group_id: str | None = None
    ) -> "Policy":
        return cls(
            roster=roster,
            allowed_labels=frozenset(allowed_labels),
            group_id=group_id,
            _name_variants=tuple(
                sorted(roster.name_variants(), key=len, reverse=True)
            ),
        )

    def phone_digit_forms(self) -> tuple[str, ...]:
        """Roster phones reduced to digits, so formatting cannot hide them."""
        out = []
        for p in self.roster.phones:
            d = _digits(p)
            if len(d) >= 7:
                out.append(d)
                out.append(d[-7:])   # national tail, written without prefix
        return tuple(out)


# --- the firewall -------------------------------------------------------------
#
# Three assertions, in this order. Everything else in this file is support.


def _assert_no_known_identity(text: str, policy: Policy, sha: str) -> None:
    """1. Nothing we already know to be identity may appear."""
    for variant in policy._name_variants:
        if re.search(rf"(?<!\w){re.escape(variant)}(?!\w)", text, re.I):
            raise EgressViolation("roster-name", "a roster name is present", sha)

    stripped = _digits(text)
    for digits in policy.phone_digit_forms():
        if digits and digits in stripped:
            raise EgressViolation("roster-phone", "a roster phone is present", sha)

    if policy.roster.group_name and re.search(
        rf"(?<!\w){re.escape(policy.roster.group_name)}(?!\w)", text, re.I
    ):
        raise EgressViolation("group-name", "the group name is present", sha)

    if policy.group_id and policy.group_id in text:
        raise EgressViolation("group-id", "the group id is present", sha)


def _assert_no_identity_shapes(text: str, sha: str) -> None:
    """2. Nothing *shaped* like identity may appear, known to us or not.

    Catches the member who was never added to the roster, and the phone number
    of a third party quoted in the chat.
    """
    for rule, pattern in (
        ("uuid", UUID_RE),
        ("email", EMAIL_RE),
        ("e164-phone", E164_RE),
    ):
        if pattern.search(text):
            raise EgressViolation(rule, f"text matching {rule} is present", sha)

    # The separated-phone shape (digits, separator, digits, separator, digits)
    # also describes an ISO date. "2026-07-14" carries 8 digits; the shortest
    # international phone number carries 9. Requiring 9 keeps real numbers
    # caught and stops the firewall blocking its own timestamp headers -- which
    # it did, on every batch, until an end-to-end run surfaced it.
    for match in SEPARATED_PHONE_RE.finditer(text):
        if len(_digits(match.group())) >= 9:
            raise EgressViolation(
                "separated-phone", "text matching separated-phone is present", sha
            )


def _assert_labels_are_allowed(text: str, policy: Policy, sha: str) -> None:
    """3. Every speaker label must be one we allocated."""
    for match in SPEAKER_RE.finditer(text):
        label = f"Participant {match.group(1)}"
        if label not in policy.allowed_labels:
            raise EgressViolation(
                "unknown-label", "an unallocated speaker label is present", sha
            )


def _assert_request_shape(payload: Any, sha: str) -> None:
    """Structural check: no key that would route content past this module."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_KEYS:
                raise EgressViolation(
                    "request-shape", f"forbidden key {key!r} in request", sha
                )
            _assert_request_shape(value, sha)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            _assert_request_shape(item, sha)


def check_outbound(payload: dict[str, Any], policy: Policy) -> str:
    """Validate a request body. Returns its sha on success, raises otherwise."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    sha = _sha(body)
    text = _normalise(body)

    _assert_request_shape(payload, sha)
    _assert_no_known_identity(text, policy, sha)
    _assert_no_identity_shapes(text, sha)
    _assert_labels_are_allowed(text, policy, sha)
    return sha


def check_inbound(text: str, policy: Policy) -> str:
    """Validate a model response before it can reach the vault.

    A model can echo back content it was given, and a response is written to a
    file other people read. Same rules, same failure mode.
    """
    sha = _sha(text)
    normalised = _normalise(text)
    _assert_no_known_identity(normalised, policy, sha)
    _assert_no_identity_shapes(normalised, sha)
    _assert_labels_are_allowed(normalised, policy, sha)
    return sha


# --- quarantine ---------------------------------------------------------------


def quarantine(payload: Any, violation: EgressViolation, directory: Path) -> Path:
    """Write a blocked payload to disk for review.

    Stays local and gitignored. Keeping it is the only way to debug a false
    positive without re-running against live data -- but it does mean the
    quarantine directory holds unredacted content, so it is covered by the
    same retention and pruning policy as the message cache.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{violation.payload_sha}-{violation.rule}.json"
    path.write_text(
        json.dumps(
            {"rule": violation.rule, "detail": violation.detail, "payload": payload},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def guard(payload: dict[str, Any], policy: Policy, quarantine_dir: Path) -> str:
    """check_outbound, quarantining on failure. The call site for the pipeline."""
    try:
        return check_outbound(payload, policy)
    except EgressViolation as violation:
        quarantine(payload, violation, quarantine_dir)
        raise
