"""Tests for the Signal Android backup importer.

The fixtures are synthetic CSVs shaped like a signalbackup-tools export. Every
identifier is assembled at runtime rather than written as a literal, per the
repo-wide invariant that no tracked file contains a contiguous UUID- or
phone-shaped string.

The property that matters most here is the group filter. A Signal backup holds
every conversation on the account, so an importer that reads the wrong rows
does not merely import too much -- it pulls unrelated private conversations
into a research archive.
"""

from __future__ import annotations

import base64
import csv
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.envelope import SELF  # noqa: E402
from signal_research_bot.importer import (  # noqa: E402
    ImportError_,
    load_acis,
    normalise_group_id,
    parse,
    resolve_thread_ids,
)

ALICE = str(uuid.UUID(int=0xA11CE))
BOB = str(uuid.UUID(int=0xB0B))
TS = 1_784_000_000_000

GROUP_BYTES = bytes(range(32))
GROUP_B64 = base64.b64encode(GROUP_BYTES).decode()
GROUP_HEX = GROUP_BYTES.hex()

OUTGOING_SENT = 23          # MessageTypes.BASE_SENT_TYPE
INCOMING_INBOX = 20         # MessageTypes.BASE_INBOX_TYPE


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def export(tmp_path):
    """A two-group export: the target group, and an unrelated private chat."""
    _write(tmp_path / "recipient.csv", [
        {"_id": "1", "aci": ALICE},
        {"_id": "2", "aci": BOB},
        {"_id": "9", "aci": ""},                     # group's own recipient row
        {"_id": "7", "aci": str(uuid.UUID(int=0xDEAD))},   # someone else entirely
    ])
    _write(tmp_path / "groups.csv", [
        {"group_id": f"__signal_group__v2__!{GROUP_HEX}", "recipient_id": "9"},
        {"group_id": f"__signal_group__v2__!{'ab' * 32}", "recipient_id": "8"},
    ])
    _write(tmp_path / "thread.csv", [
        {"_id": "100", "recipient_id": "9"},         # target group
        {"_id": "200", "recipient_id": "8"},         # other group
        {"_id": "300", "recipient_id": "7"},         # a 1:1 chat
    ])
    _write(tmp_path / "message.csv", [
        {"thread_id": "100", "from_recipient_id": "1", "date_sent": str(TS),
         "body": "is the attestation an audit?", "type": str(INCOMING_INBOX),
         "remote_deleted": "0"},
        {"thread_id": "100", "from_recipient_id": "9", "date_sent": str(TS + 1000),
         "body": "my own reply", "type": str(OUTGOING_SENT), "remote_deleted": "0"},
        {"thread_id": "200", "from_recipient_id": "2", "date_sent": str(TS + 2000),
         "body": "OTHER GROUP CONTENT", "type": str(INCOMING_INBOX),
         "remote_deleted": "0"},
        {"thread_id": "300", "from_recipient_id": "7", "date_sent": str(TS + 3000),
         "body": "PRIVATE ONE TO ONE CONTENT", "type": str(INCOMING_INBOX),
         "remote_deleted": "0"},
        {"thread_id": "100", "from_recipient_id": "2", "date_sent": str(TS + 4000),
         "body": "", "type": str(INCOMING_INBOX), "remote_deleted": "0"},
        {"thread_id": "100", "from_recipient_id": "2", "date_sent": str(TS + 5000),
         "body": "deleted for everyone", "type": str(INCOMING_INBOX),
         "remote_deleted": "1"},
    ])
    return tmp_path


# --- the group filter --------------------------------------------------------


def test_only_the_target_group_is_imported(export):
    """A Signal backup holds every conversation on the account. Reading the
    wrong rows imports unrelated private chats into a research archive."""
    messages, stats = parse(export, GROUP_BYTES)
    bodies = [m.body for m in messages]

    assert "OTHER GROUP CONTENT" not in bodies
    assert "PRIVATE ONE TO ONE CONTENT" not in bodies
    assert stats.other_group == 2


def test_the_group_filter_runs_before_any_other_field_is_read(export):
    """Ordering, not just outcome.

    The receiver shipped this exact bug on 2026-07-26: it read sourceName off
    every envelope and only afterwards checked the group, harvesting 41 display
    names from the whole account. Here the stakes are higher, so a row from
    another chat must be counted and abandoned before its body, sender or
    timestamp is touched.
    """
    _write(export / "message.csv", [
        # Deliberately malformed everywhere EXCEPT thread_id. If any other
        # field were read first this would raise rather than be skipped.
        {"thread_id": "300", "from_recipient_id": "", "date_sent": "not-a-number",
         "body": "PRIVATE", "type": "garbage", "remote_deleted": "?"},
        {"thread_id": "100", "from_recipient_id": "1", "date_sent": str(TS),
         "body": "in scope", "type": str(INCOMING_INBOX), "remote_deleted": "0"},
    ])
    messages, stats = parse(export, GROUP_BYTES)
    assert [m.body for m in messages] == ["in scope"]
    assert stats.other_group == 1


def test_an_unmatched_group_id_fails_loudly(export):
    """Silently importing nothing would look like an empty backup."""
    with pytest.raises(ImportError_, match="not found in groups.csv"):
        parse(export, b"\xff" * 32)


# --- identity ----------------------------------------------------------------


def test_senders_keep_their_real_aci(export):
    """The whole reason for using the CSV dump rather than a rendered export.

    PseudonymStore keys on the ACI, so an import keyed on display names would
    give the same person a different label from the live pipeline.
    """
    messages, _ = parse(export, GROUP_BYTES)
    incoming = [m for m in messages if m.source != SELF]
    assert incoming and all(m.source == ALICE for m in incoming)


def test_outgoing_messages_use_the_same_self_sentinel_as_the_live_receiver(export):
    """Own messages arrive live as syncMessage and are labelled SELF. An import
    that used the operator's ACI instead would allocate them a second, separate
    participant label."""
    messages, _ = parse(export, GROUP_BYTES)
    assert any(m.source == SELF and m.body == "my own reply" for m in messages)


def test_a_sender_with_no_aci_is_dropped_not_guessed(export):
    _write(export / "recipient.csv", [
        {"_id": "1", "aci": ""},
        {"_id": "9", "aci": ""},
    ])
    messages, stats = parse(export, GROUP_BYTES)
    assert not [m for m in messages if m.source != SELF]
    assert stats.unknown_sender >= 1


# --- content rules mirror the live path --------------------------------------


def test_remotely_deleted_messages_are_not_imported(export):
    """The sender deleted it for everyone. The live path drops these, and an
    import that resurrected them would undo a deletion the group was promised."""
    messages, stats = parse(export, GROUP_BYTES)
    assert "deleted for everyone" not in [m.body for m in messages]
    assert stats.remote_deleted == 1


def test_bodyless_rows_are_skipped(export):
    """Call logs, group updates and attachment-only rows: no research value,
    and they would only cost tokens."""
    _, stats = parse(export, GROUP_BYTES)
    assert stats.no_body == 1


def test_since_filter_bounds_the_import(export):
    messages, _ = parse(export, GROUP_BYTES, since_ms=TS + 500)
    assert all(m.raw_timestamp_ms >= TS + 500 for m in messages)
    assert messages


# --- group id forms ----------------------------------------------------------


@pytest.mark.parametrize("written", [
    GROUP_B64,
    GROUP_HEX,
    f"__signal_group__v2__!{GROUP_HEX}",
    f"__SIGNAL_GROUP__V2__!{GROUP_HEX.upper()}",
])
def test_every_written_form_of_a_group_id_reduces_to_the_same_bytes(written):
    """`.env` holds base64 from signal-cli's JSON-RPC; the Android backup holds
    hex behind a prefix. Comparing strings finds nothing."""
    assert normalise_group_id(written) == GROUP_BYTES


def test_hex_is_not_mistaken_for_base64():
    """A hex string is also valid base64 input and would decode to different
    bytes without complaint -- a silent wrong-group match."""
    assert normalise_group_id(GROUP_HEX) == GROUP_BYTES
    assert normalise_group_id(GROUP_HEX) != base64.b64decode(GROUP_HEX + "==")


# --- schema tolerance --------------------------------------------------------


def test_older_column_names_are_accepted(export):
    """Signal renamed recipient_id -> from_recipient_id and uuid -> aci across
    versions, and the export reflects whatever version made the backup."""
    _write(export / "recipient.csv", [
        {"_id": "1", "uuid": ALICE},
        {"_id": "9", "uuid": ""},
    ])
    _write(export / "message.csv", [
        {"thread_id": "100", "recipient_id": "1", "date": str(TS),
         "body": "older schema", "type": str(INCOMING_INBOX)},
    ])
    messages, _ = parse(export, GROUP_BYTES)
    assert [m.body for m in messages] == ["older schema"]
    assert messages[0].source == ALICE


def test_a_missing_column_fails_loudly_rather_than_importing_blanks(export):
    """Guessing would produce a window of empty strings and an empty run that
    looks like a quiet day."""
    _write(export / "recipient.csv", [{"_id": "1", "nickname": "who"}])
    with pytest.raises(ImportError_, match="none of"):
        load_acis(export)


def test_thread_resolution_is_exact(export):
    assert resolve_thread_ids(export, GROUP_BYTES) == {"100"}
