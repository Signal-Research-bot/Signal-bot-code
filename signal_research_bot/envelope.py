"""Parse signal-cli JSON-RPC envelopes into a normalised internal form.

This module is deliberately pure: it does no I/O, holds no state, and makes no
network calls. Everything it needs arrives as an argument. That makes the four
traps below testable in isolation, which matters because every one of them
fails *silently* -- producing plausible, wrong output rather than an error.

See .claude/skills/signal-envelope for the reasoning behind each.

  1. Own messages arrive as syncMessage.sentMessage, not dataMessage.
  2. Mention offsets are UTF-16 code units, not codepoints.
  3. Disappearing messages must be hard-dropped, never cached.
  4. Messages mutate: remoteDelete and editMessage need a retraction path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Signal timestamps are milliseconds. A millisecond timestamp plus a message
# length is a near-perfect join key against anyone else's copy of the chat, so
# everything downstream sees a coarsened value. 15 minutes is the plan's figure.
TIMESTAMP_GRANULARITY_MS = 15 * 60 * 1000

SELF = "self"  # sentinel source id for the operator's own messages


class Kind(str, Enum):
    MESSAGE = "message"
    DELETE = "delete"          # sender deleted for everyone
    EDIT = "edit"              # sender edited a previous message
    EXPIRATION_UPDATE = "expiration_update"


@dataclass(frozen=True)
class Mention:
    """A mention. `start` and `length` are UTF-16 code units, as Signal sends."""

    start: int
    length: int
    uuid: str


@dataclass(frozen=True)
class ParsedMessage:
    kind: Kind
    group_id: str
    source: str                      # raw ACI uuid, or SELF
    timestamp_ms: int                # coarsened; see TIMESTAMP_GRANULARITY_MS
    body: str = ""
    mentions: tuple[Mention, ...] = ()
    quote_author: str | None = None
    quote_text: str | None = None
    attachment_count: int = 0        # count only: filenames are identity-bearing
    target_timestamp_ms: int | None = None   # for DELETE / EDIT
    raw_timestamp_ms: int = 0        # uncoarsened, for dedupe only -- never sent

    @property
    def dedupe_key(self) -> tuple[str, int]:
        """Stable identity for a message observed more than once.

        Uses the *raw* timestamp: coarsening to 15 minutes would collapse
        distinct messages from the same sender into one key.
        """
        return (self.source, self.raw_timestamp_ms)


class DisappearingMessage(Exception):
    """Raised when a message has a non-null expiry. Callers must drop it.

    An exception rather than a None return: dropping this content is a policy
    decision the caller should have to acknowledge, not something that can be
    silently ignored by a caller that forgot to check.
    """


def coarsen(timestamp_ms: int) -> int:
    """Floor a millisecond timestamp to the retention granularity."""
    return (timestamp_ms // TIMESTAMP_GRANULARITY_MS) * TIMESTAMP_GRANULARITY_MS


def _payload(envelope: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Return (message payload, source id) handling the sync/data split.

    TRAP 1. signal-cli runs as a *linked device on the operator's own account*,
    so messages the operator sends come back as syncMessage.sentMessage with the
    dataMessage fields inlined (@JsonUnwrapped on the Java side). The intuitive
    filter -- "ignore syncMessage, it's device-sync noise" -- discards 100% of
    the operator's own contributions, and the operator is usually the most
    active participant in a group they built a research bot for.
    """
    data = envelope.get("dataMessage")
    if isinstance(data, dict):
        return data, envelope.get("sourceUuid") or envelope.get("source") or ""

    # An edit from another member arrives as an envelope-level `editMessage`
    # wrapping its own dataMessage -- there is no top-level dataMessage at all.
    # Checking only the two branches above dropped every edit anyone else made,
    # silently, leaving the un-edited original as the archived version. The
    # kind/target detection further down already knew about editMessage; it just
    # never ran, because this function returned None first.
    edit = envelope.get("editMessage")
    if isinstance(edit, dict) and isinstance(edit.get("dataMessage"), dict):
        return (
            edit["dataMessage"],
            envelope.get("sourceUuid") or envelope.get("source") or "",
        )

    sync = envelope.get("syncMessage") or {}
    sent = sync.get("sentMessage")
    if isinstance(sent, dict):
        # The operator's own edit: syncMessage.sentMessage.editMessage, or
        # syncMessage.editMessage depending on the signal-cli path.
        inner = sent.get("editMessage")
        if isinstance(inner, dict) and isinstance(inner.get("dataMessage"), dict):
            return inner["dataMessage"], SELF
        return sent, SELF

    sync_edit = sync.get("editMessage")
    if isinstance(sync_edit, dict):
        nested = sync_edit.get("dataMessage") or (
            sync_edit.get("sentMessage") or {}
        ).get("dataMessage")
        if isinstance(nested, dict):
            return nested, SELF

    return None, ""


def _edit_target(envelope: dict[str, Any], payload: dict[str, Any]) -> int | None:
    """The timestamp of the message an edit supersedes, if this is an edit."""
    sync = envelope.get("syncMessage") or {}
    for holder in (
        payload,
        envelope.get("editMessage"),
        (sync.get("sentMessage") or {}).get("editMessage"),
        sync.get("editMessage"),
    ):
        if not isinstance(holder, dict):
            continue
        target = holder.get("editTargetTimestamp") or holder.get("targetSentTimestamp")
        if target:
            return int(target)
    return None


def _mentions(payload: dict[str, Any]) -> tuple[Mention, ...]:
    out = []
    for m in payload.get("mentions") or ():
        uuid = m.get("uuid") or m.get("author") or ""
        try:
            out.append(Mention(int(m["start"]), int(m["length"]), uuid))
        except (KeyError, TypeError, ValueError):
            continue  # malformed mention: drop it rather than mis-slice the body
    return tuple(out)


def parse(envelope: dict[str, Any], target_group_id: str) -> ParsedMessage | None:
    """Normalise one envelope, or return None if it is not for us.

    Raises DisappearingMessage if the message carries an expiry timer.
    """
    payload, source = _payload(envelope)
    if payload is None:
        return None

    group = (payload.get("groupInfo") or {}).get("groupId")
    if group != target_group_id:
        return None

    # TRAP 3. Disappearing messages are hard-dropped. Persisting content into a
    # permanent knowledge base after the senders configured it to vanish
    # contradicts their explicit intent, and the envelope tells us plainly.
    if payload.get("expiresInSeconds"):
        raise DisappearingMessage(f"expiresInSeconds={payload['expiresInSeconds']}")

    raw_ts = int(envelope.get("timestamp") or payload.get("timestamp") or 0)
    ts = coarsen(raw_ts)

    # TRAP 4. Mutation events. These are not messages; they retract or supersede
    # one. The cache and the KB both need to act on them.
    if payload.get("isExpirationUpdate"):
        return ParsedMessage(Kind.EXPIRATION_UPDATE, group, source, ts, raw_timestamp_ms=raw_ts)

    remote = payload.get("remoteDelete")
    if isinstance(remote, dict):
        return ParsedMessage(
            Kind.DELETE, group, source, ts,
            target_timestamp_ms=int(remote.get("timestamp") or 0),
            raw_timestamp_ms=raw_ts,
        )

    quote = payload.get("quote") or {}
    kind = Kind.MESSAGE
    target = _edit_target(envelope, payload)
    if target is not None:
        kind = Kind.EDIT

    return ParsedMessage(
        kind=kind,
        group_id=group,
        source=source,
        timestamp_ms=ts,
        body=payload.get("message") or "",
        mentions=_mentions(payload),
        quote_author=quote.get("authorUuid") or quote.get("author"),
        quote_text=quote.get("text"),
        # Filenames are identity-bearing ("holiday-with-<name>.jpg"). Keep the
        # count so a transcript can note that an attachment existed.
        attachment_count=len(payload.get("attachments") or ()),
        target_timestamp_ms=target,
        raw_timestamp_ms=raw_ts,
    )


def substitute_mentions(body: str, mentions: tuple[Mention, ...], label_for) -> str:
    """Replace mention spans with pseudonymous labels.

    TRAP 2. `start` and `length` are UTF-16 code units, because Signal's
    reference implementation is Java and Java strings are UTF-16 (signal-cli
    #1504). Python strings are sequences of codepoints, so the natural
    `body[start:start + length]` drifts by one per non-BMP character -- most
    emoji -- appearing earlier in the message. The result still looks like a
    sentence, just with a character eaten, which is why this needs a test rather
    than a code review.

    `label_for` maps a Mention to its replacement text.

    Offsets arrive over the network and are attacker-controlled. Three ways a
    crafted mention breaks a naive implementation, all found by audit:

    * A `start` that lands *inside* a surrogate pair splits an emoji in half.
      The remaining lone surrogate is not decodable, so `.decode()` raises
      UnicodeDecodeError -- which, in the receive loop, stalls the pipeline on
      every subsequent poll because the message is redelivered forever.
    * A negative `length` makes `hi < lo`, and `buf[:lo] + label + buf[hi:]`
      then *duplicates* the bytes between them instead of replacing anything.
    * Overlapping spans let a later substitution land inside the label written
      by an earlier one.

    Each is handled by skipping that mention. A mention we cannot apply is
    strictly better left as raw text: the body still passes redaction and the
    egress firewall afterwards, so a skipped mention cannot leak an identity.
    """
    if not mentions:
        return body

    buf = body.encode("utf-16-le")
    limit = len(buf)

    def splits_surrogate(offset: int) -> bool:
        """True if `offset` falls between the halves of a surrogate pair."""
        if offset <= 0 or offset >= limit:
            return False
        # A UTF-16-LE high surrogate is 0xD800-0xDBFF, stored little-endian, so
        # the *second* byte of the unit carries the tag.
        unit = int.from_bytes(buf[offset - 2 : offset], "little")
        return 0xD800 <= unit <= 0xDBFF

    # Descending, so an earlier replacement cannot invalidate a later offset.
    consumed = limit  # low-water mark of what later (higher) spans already took
    for m in sorted(mentions, key=lambda m: m.start, reverse=True):
        # Rejecting a negative start or length here makes `hi < lo` unreachable
        # below, so there is no separate inverted-span check: dead code in a
        # parser handling hostile input is worse than the redundancy is worth,
        # and mutation testing flags it as an untestable branch.
        if m.length < 0 or m.start < 0:
            continue
        lo, hi = m.start * 2, (m.start + m.length) * 2
        if hi > limit:
            continue  # the span runs past the end of the body
        if hi > consumed:
            continue  # overlaps a span already substituted
        if splits_surrogate(lo) or splits_surrogate(hi):
            continue  # would leave an undecodable lone surrogate
        buf = buf[:lo] + label_for(m).encode("utf-16-le") + buf[hi:]
        consumed = lo

    try:
        return buf.decode("utf-16-le")
    except UnicodeDecodeError:
        # Belt and braces. The guards above should make this unreachable, but
        # the alternative to a lossy decode here is a poison message that stalls
        # the receiver forever, and that trade is not close.
        return buf.decode("utf-16-le", errors="replace")


@dataclass
class Deduper:
    """Drops messages already seen. The same logical message can arrive twice."""

    _seen: set[tuple[str, int]] = field(default_factory=set)

    def is_new(self, msg: ParsedMessage) -> bool:
        key = msg.dedupe_key
        if key in self._seen:
            return False
        self._seen.add(key)
        return True
