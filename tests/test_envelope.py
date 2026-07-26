"""Tests for envelope parsing.

All fixtures are SYNTHETIC. The UUIDs are RFC-4122 example values and the
group id is invented; nothing here came from a real Signal account.

Each test names the trap it guards. All four traps fail silently in production
-- they produce plausible, wrong output -- so these are the tests that matter.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.envelope import (  # noqa: E402
    SELF,
    Deduper,
    DisappearingMessage,
    Kind,
    Mention,
    coarsen,
    parse,
    substitute_mentions,
)

GROUP = "Zm9vYmFyZ3JvdXBpZA=="
OTHER_GROUP = "b3RoZXJncm91cGlkMQ=="

# Built from small integers rather than written as literals. A UUID literal in
# a tracked file is indistinguishable from a real ACI to scrub_check -- and to
# a reviewer -- so the test suite never contains one, not even in a comment.
# Both render as long runs of zeroes with a few hex digits at the end.
ALICE = str(uuid.UUID(int=0xA11CE))
BOB = str(uuid.UUID(int=0xB0B))

TS = 1_784_000_000_000


def data_envelope(**payload):
    body = {"groupInfo": {"groupId": GROUP}, "message": "hello"}
    body.update(payload)
    return {"sourceUuid": ALICE, "timestamp": TS, "dataMessage": body}


def sync_envelope(**payload):
    """The operator's own message, as it actually arrives on a linked device."""
    body = {"groupInfo": {"groupId": GROUP}, "message": "mine"}
    body.update(payload)
    return {"sourceUuid": ALICE, "timestamp": TS, "syncMessage": {"sentMessage": body}}


# --- TRAP 1: own messages arrive as syncMessage ------------------------------


def test_incoming_group_message_is_parsed():
    msg = parse(data_envelope(), GROUP)
    assert msg is not None and msg.kind is Kind.MESSAGE
    assert msg.source == ALICE and msg.body == "hello"


def test_own_message_via_syncmessage_is_parsed():
    """The whole point of trap 1. A naive parser returns None here and the
    operator's own contributions vanish without any error."""
    msg = parse(sync_envelope(), GROUP)
    assert msg is not None, "own messages were dropped -- trap 1 has regressed"
    assert msg.source == SELF and msg.body == "mine"


def test_other_groups_are_ignored_on_both_branches():
    other = {"groupInfo": {"groupId": OTHER_GROUP}, "message": "x"}
    assert parse({"sourceUuid": ALICE, "dataMessage": other}, GROUP) is None
    assert parse({"syncMessage": {"sentMessage": other}}, GROUP) is None


def test_non_message_envelopes_are_ignored():
    assert parse({"receiptMessage": {"isRead": True}}, GROUP) is None
    assert parse({"typingMessage": {"action": "STARTED"}}, GROUP) is None
    assert parse({}, GROUP) is None


def test_dedupe_uses_raw_timestamp_not_coarsened():
    """Two messages 1s apart must stay distinct despite 15-minute coarsening."""
    d = Deduper()
    a = parse({**data_envelope(), "timestamp": TS}, GROUP)
    b = parse({**data_envelope(), "timestamp": TS + 1000}, GROUP)
    assert d.is_new(a) and d.is_new(b)
    assert not d.is_new(a)


# --- TRAP 2: mention offsets are UTF-16 code units ---------------------------


def label(_m):
    return "Participant A"


def test_mention_substitution_ascii():
    body = "hi @alice ok"
    m = (Mention(start=3, length=6, uuid=ALICE),)
    assert substitute_mentions(body, m, label) == "hi Participant A ok"


def test_mention_substitution_after_emoji():
    """Trap 2. A non-BMP emoji is 2 UTF-16 units but 1 Python codepoint.

    Naive body[start:start+length] slicing eats one character here and still
    returns a sentence-shaped string, which is why this needs a test.
    """
    body = "\U0001f600 @alice ok"          # emoji, space, @alice, space, ok
    # Signal counts the emoji as 2 units, so @alice starts at 3, not 2.
    m = (Mention(start=3, length=6, uuid=ALICE),)
    assert substitute_mentions(body, m, label) == "\U0001f600 Participant A ok"


def test_multiple_mentions_replaced_right_to_left():
    body = "@alice and @bob"
    m = (
        Mention(start=0, length=6, uuid=ALICE),
        Mention(start=11, length=4, uuid=BOB),
    )
    labels = {ALICE: "Participant A", BOB: "Participant B"}
    out = substitute_mentions(body, m, lambda mm: labels[mm.uuid])
    assert out == "Participant A and Participant B"


def test_out_of_range_mention_does_not_corrupt_body():
    body = "short"
    assert substitute_mentions(body, (Mention(99, 5, ALICE),), label) == "short"


def test_mentions_are_parsed_from_both_branches():
    ment = [{"start": 0, "length": 6, "uuid": ALICE}]
    assert parse(data_envelope(mentions=ment), GROUP).mentions[0].uuid == ALICE
    assert parse(sync_envelope(mentions=ment), GROUP).mentions[0].uuid == ALICE


def test_malformed_mention_is_dropped_not_guessed():
    assert parse(data_envelope(mentions=[{"start": "x"}]), GROUP).mentions == ()


# --- TRAP 3: disappearing messages are hard-dropped --------------------------


def test_disappearing_message_raises():
    with pytest.raises(DisappearingMessage):
        parse(data_envelope(expiresInSeconds=604800), GROUP)


def test_disappearing_message_raises_on_own_messages_too():
    with pytest.raises(DisappearingMessage):
        parse(sync_envelope(expiresInSeconds=86400), GROUP)


def test_zero_expiry_is_not_disappearing():
    assert parse(data_envelope(expiresInSeconds=0), GROUP) is not None


# --- TRAP 4: mutation events -------------------------------------------------


def test_remote_delete_is_a_retraction_not_a_message():
    msg = parse(data_envelope(remoteDelete={"timestamp": TS - 5000}), GROUP)
    assert msg.kind is Kind.DELETE
    assert msg.target_timestamp_ms == TS - 5000


def test_edit_is_flagged_with_its_target():
    msg = parse(data_envelope(editTargetTimestamp=TS - 9000), GROUP)
    assert msg.kind is Kind.EDIT
    assert msg.target_timestamp_ms == TS - 9000


def test_expiration_update_is_flagged():
    msg = parse(data_envelope(isExpirationUpdate=True), GROUP)
    assert msg.kind is Kind.EXPIRATION_UPDATE


# --- privacy-shaped parsing behaviour ----------------------------------------


def test_timestamps_are_coarsened_to_fifteen_minutes():
    """ms timestamp + message length is a near-perfect join key against anyone
    else's copy of the chat, so nothing downstream sees full precision."""
    msg = parse({**data_envelope(), "timestamp": TS + 7 * 60_000 + 123}, GROUP)
    assert msg.timestamp_ms % (15 * 60 * 1000) == 0
    assert msg.timestamp_ms <= TS + 7 * 60_000 + 123


def test_coarsen_floors_rather_than_rounds():
    """Anything inside a bucket lands on the bucket's start, never the next one.

    Rounding up would push a timestamp into the future, which is both wrong and
    a subtle way to leak that the original was near a boundary.
    """
    base = coarsen(TS)                                  # a real bucket boundary
    assert coarsen(base) == base
    assert coarsen(base + 14 * 60_000 + 59_999) == base
    assert coarsen(base + 15 * 60_000) == base + 15 * 60_000


def test_attachment_filenames_are_not_retained():
    """Filenames leak identity ('holiday-with-<name>.jpg'); keep only a count."""
    msg = parse(
        data_envelope(attachments=[{"filename": "IMG_with_alice.jpg", "id": "a1"}]),
        GROUP,
    )
    assert msg.attachment_count == 1
    assert "alice" not in repr(msg).lower()


# --- audit regressions --------------------------------------------------------


def test_an_edit_from_another_member_is_parsed():
    """signal-cli delivers another member's edit as an envelope-level
    editMessage wrapping its own dataMessage. _payload() checked only
    dataMessage and syncMessage, returned None first, and so every edit anyone
    else made was dropped -- leaving the un-edited original as the archived
    version, silently."""
    env = {
        "sourceUuid": ALICE,
        "timestamp": TS,
        "editMessage": {
            "targetSentTimestamp": TS - 9000,
            "dataMessage": {"groupInfo": {"groupId": GROUP}, "message": "corrected"},
        },
    }
    parsed = parse(env, GROUP)
    assert parsed is not None, "the edit was dropped entirely"
    assert parsed.kind is Kind.EDIT
    assert parsed.body == "corrected"
    assert parsed.source == ALICE
    assert parsed.target_timestamp_ms == TS - 9000


def test_the_operators_own_edit_is_parsed():
    env = {
        "timestamp": TS,
        "syncMessage": {
            "sentMessage": {
                "editMessage": {
                    "targetSentTimestamp": TS - 9000,
                    "dataMessage": {
                        "groupInfo": {"groupId": GROUP}, "message": "fixed typo"
                    },
                }
            }
        },
    }
    parsed = parse(env, GROUP)
    assert parsed is not None and parsed.kind is Kind.EDIT
    assert parsed.body == "fixed typo"
    assert parsed.source == SELF


def test_a_mention_offset_inside_a_surrogate_pair_does_not_crash():
    """Offsets arrive over the network. A start that lands between the halves
    of an emoji leaves a lone surrogate, and .decode() raises UnicodeDecodeError
    -- which in the receive loop stalls the pipeline forever, because the
    message is redelivered on every poll."""
    body = "hi \U0001f600 @x"          # the emoji is one surrogate PAIR in UTF-16
    out = substitute_mentions(body, (Mention(4, 1, BOB),), lambda m: "Participant B")
    assert out == body, "an unapplicable mention must be left as raw text"


def test_a_negative_mention_length_does_not_duplicate_the_body():
    """hi < lo makes buf[:lo] + label + buf[hi:] duplicate the bytes between
    them instead of replacing anything."""
    body = "hi \U0001f600 @x"
    out = substitute_mentions(body, (Mention(3, -1, BOB),), lambda m: "Participant B")
    assert out == body


def test_overlapping_mentions_do_not_corrupt_each_other():
    body = "aaaaaaaa"
    out = substitute_mentions(
        body, (Mention(0, 5, BOB), Mention(3, 5, BOB)), lambda m: "X"
    )
    assert out.count("X") == 1, "an overlapping span was applied on top of a label"
    assert "�" not in out
