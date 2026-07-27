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
# The bare-digit-run rule and its validity check live in redact.py, where
# libphonenumber is already a hard dependency. Importing them keeps one
# definition of "is this a phone number" rather than two that can drift.
from .redact import BARE_DIGIT_RUN_RE, is_dialable, looks_like_phone

# Characters that render as nothing -- or that reorder what is rendered --
# but break a naive substring match. The bidi controls are here because they
# are equally effective at it: RLO inside a name survives NFKC, renders as
# nothing on its own, and splits the token for every regex in this file.
_INVISIBLE = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x034F, 0x180E]
    + list(range(0x200E, 0x2010))          # LRM, RLM, and the bidi marks
    + list(range(0x202A, 0x202F))          # LRE, RLE, PDF, LRO, RLO
    + list(range(0x2066, 0x206A))          # LRI, RLI, FSI, PDI
    + list(range(0xFFF9, 0xFFFC)),         # interlinear annotation
    None,
)

SPEAKER_RE = re.compile(r"\bParticipant\s+([A-Z]{1,3})\b")
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+(?![\w.-])")
UUID_RE = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])",
    re.I,
)
# A Signal ACI with the dashes removed. Both UUID rules required the dashes, so
# the raw 32-hex form -- which is how a UUID appears in a URL, a log line, or
# anything that pasted it through a system that strips punctuation -- passed
# straight through. An ACI is the one identifier the pseudonym scheme is built
# on, so it is the last thing that should leave in any form.
UUID_COMPACT_RE = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{32}(?![0-9a-zA-Z])", re.I)
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
    folded = folded.replace("\\", "/")
    if folded.isascii():
        return folded
    # Non-ASCII decimal digits. NFKC folds fullwidth digits but NOT Arabic-Indic,
    # Devanagari, or the dozen other decimal scripts -- and every phone rule
    # below matches ASCII `\d`. Without this, a phone number typed in Eastern
    # Arabic numerals passes the firewall untouched.
    out = []
    for ch in folded:
        if ch.isdigit() and not ch.isascii():
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(ch)
    return "".join(out)


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()[:12]


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)


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
            # name_variants(), NOT redaction_variants(): chat handles are
            # deliberately excluded from the firewall.
            #
            # This module matches every variant, case-insensitively, against the
            # serialized request -- which contains this bot's own system
            # prompts. A member whose handle is an ordinary word ("gate",
            # "money", "audit") therefore matches the prompt text itself, and
            # because batch.py consumes a firewall-blocked window rather than
            # retrying it, every window would fail forever. "audit" collides
            # with both the extract and triage prompts as shipped.
            #
            # Handles are enforced in redact.py instead, where a miss costs one
            # un-redacted pseudonym rather than the whole tool.
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

    def would_block(self, text: str) -> bool:
        """Would this fragment trip the identity rules? Asks, does not raise.

        For content the pipeline ASSEMBLES rather than receives -- specifically
        the archive index, which is built from the filenames of pages a human
        wrote. Those filenames are not message content and were never redacted,
        so one page named after a group member would fail the firewall on the
        way out, and a blocked window is consumed rather than retried: a single
        unlucky filename would silently end every future window.

        The firewall is not weakened by this. It still runs, unchanged, over the
        whole serialised body. This just lets a caller drop the one line it
        assembled rather than lose the window, and the caller counts what it
        dropped.
        """
        folded = _normalise(text)
        return any(
            re.search(rf"(?<!\w){re.escape(v)}(?!\w)", folded, re.I)
            for v in self._name_variants
        ) or bool(
            self.roster.group_name
            and re.search(
                rf"(?<!\w){re.escape(self.roster.group_name)}(?!\w)", folded, re.I
            )
        )


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
        ("uuid-compact", UUID_COMPACT_RE),
        ("email", EMAIL_RE),
        ("e164-phone", E164_RE),
    ):
        if pattern.search(text):
            raise EgressViolation(rule, f"text matching {rule} is present", sha)

    # The separated-phone shape also describes an ISO date, a SEC CIK and an
    # EDGAR accession number. The digit-count floor of 9 handled the date; it
    # did nothing for the filing identifiers, which are longer, not shorter.
    #
    # This was the most damaging defect found before launch. `0001193125-24-
    # 206789` matched, so the firewall blocked the window -- and because a
    # blocked window is CONSUMED rather than retried, one EDGAR link in a chat
    # about company filings destroyed that entire batch of messages. Two
    # separately reasonable decisions that combined into silent data loss.
    #
    # looks_like_phone() applies libphonenumber rather than a digit count, so an
    # 18-digit accession number is rejected on length alone before anything else
    # runs.
    for match in SEPARATED_PHONE_RE.finditer(text):
        if looks_like_phone(match.group()):
            raise EgressViolation(
                "separated-phone", "text matching separated-phone is present", sha
            )

    # A bare run of digits with no '+' and no separators is a phone number that
    # every rule above misses -- E164_RE requires the '+', SEPARATED_PHONE_RE
    # requires a separator. It is only blocked when libphonenumber says the run
    # is a real assignable number, because this archive is full of large figures
    # that are not phone numbers and blocking those would wedge every window
    # that discusses a balance sheet.
    for match in BARE_DIGIT_RUN_RE.finditer(text):
        if is_dialable(match.group(1)):
            raise EgressViolation(
                "bare-phone", "text matching a dialable number is present", sha
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
