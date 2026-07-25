"""Tests for the Signal notifier.

Driven against a real in-process TCP server, matching test_receiver.py: what
matters here is the wire behaviour and the loop-prevention marker.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.notify import (  # noqa: E402
    ANNOUNCEMENT,
    Notifier,
    SendFailed,
    format_summary,
)
from signal_research_bot.receiver import BOT_MARKER  # noqa: E402

GROUP = "Zm9vYmFyZ3JvdXBpZGxvbmdlbm91Z2g9PQ=="


class FakeDaemon:
    """Accepts one frame, replies with a canned response."""

    def __init__(self, reply: bytes = b'{"jsonrpc":"2.0","id":"notify","result":{}}\n'):
        self.reply = reply
        self.received: list[dict] = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                try:
                    while b"\n" not in buf:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                    if buf.strip():
                        self.received.append(json.loads(buf.split(b"\n")[0]))
                    if self.reply:
                        conn.sendall(self.reply)
                except OSError:
                    pass

    def close(self):
        self.sock.close()


@pytest.fixture
def daemon():
    d = FakeDaemon()
    yield d
    d.close()


# --- the loop-prevention marker ----------------------------------------------


def test_every_message_carries_the_bot_marker(daemon):
    """Without this the next window ingests this summary and summarises it."""
    Notifier("127.0.0.1", daemon.port, GROUP).send("hello")
    assert BOT_MARKER in daemon.received[0]["params"]["message"]


def test_announcement_carries_the_marker(daemon):
    Notifier("127.0.0.1", daemon.port, GROUP).announce()
    assert BOT_MARKER in daemon.received[0]["params"]["message"]


def test_send_targets_the_configured_group(daemon):
    Notifier("127.0.0.1", daemon.port, GROUP).send("hi")
    assert daemon.received[0]["params"]["groupId"] == GROUP
    assert daemon.received[0]["method"] == "send"


# --- the announcement says the uncomfortable parts ----------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        # scrub-ok: privacy-word-overclaim
        "pseudonymisation, not anonymisation",   # the core distinction
        "opt out",                               # the remedy
        "Disappearing messages are never processed",
        "under my name",                         # no bot identity on a linked device
        "source code is public",
        "typing [research-bot]",                 # the per-message opt-out
    ],
)
def test_announcement_states_what_members_need_to_know(phrase):
    assert phrase.lower() in ANNOUNCEMENT.lower()


# --- summaries carry counts, not content --------------------------------------


def test_quiet_window_posts_nothing():
    """A bot that says '0 new entries' nightly trains everyone to ignore it."""
    assert format_summary({"written": 0, "deferred_over_cap": 0}, []) is None


def test_summary_reports_written_entries():
    text = format_summary({"written": 2}, ["Research - a - 2026-07", "Research - b - 2026-07"])
    assert "2 new entries" in text and "Research - a - 2026-07" in text


def test_singular_entry_reads_correctly():
    assert "1 new entry." in format_summary({"written": 1}, ["Research - a - 2026-07"])


def test_deferred_tasks_are_surfaced_not_hidden():
    """The cap is the main cost lever; silent truncation reads as 'nothing missed'."""
    text = format_summary({"written": 1, "deferred_over_cap": 3}, ["t"])
    assert "3 question(s) were deferred" in text


def test_failures_are_surfaced():
    text = format_summary({"written": 1, "failed": 2}, ["t"])
    assert "2 task(s) failed" in text


def test_long_title_lists_are_truncated_with_a_count():
    titles = [f"Research - {i} - 2026-07" for i in range(15)]
    text = format_summary({"written": 15}, titles)
    assert "and 5 more" in text


def test_summary_contains_no_participant_labels():
    """A summary is broadcast, so it holds the least of anything here."""
    text = format_summary({"written": 1, "deferred_over_cap": 2, "failed": 1}, ["t"])
    assert "Participant" not in text


# --- failure handling ---------------------------------------------------------


def test_unreachable_daemon_raises_sendfailed():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(SendFailed):
        Notifier("127.0.0.1", port, GROUP).send("hi")


def test_error_response_raises_sendfailed():
    d = FakeDaemon(reply=b'{"jsonrpc":"2.0","id":"notify","error":{"code":-32602}}\n')
    try:
        with pytest.raises(SendFailed):
            Notifier("127.0.0.1", d.port, GROUP).send("hi")
    finally:
        d.close()


def test_error_message_does_not_echo_the_body():
    d = FakeDaemon(reply=b'{"jsonrpc":"2.0","id":"notify","error":{"code":-32602}}\n')
    try:
        with pytest.raises(SendFailed) as exc:
            Notifier("127.0.0.1", d.port, GROUP).send("a private summary")
        assert "private summary" not in str(exc.value)
    finally:
        d.close()


def test_disabled_notifier_sends_nothing(daemon):
    assert Notifier("127.0.0.1", daemon.port, GROUP, enabled=False).send("hi") is False
    assert daemon.received == []


def test_marker_is_visible_ascii(capsys):
    """Regression: an invisible marker crashed on a cp1252 Windows console and
    hid from members which messages were bot-generated."""
    assert BOT_MARKER.isascii()
    assert BOT_MARKER.strip() == BOT_MARKER and BOT_MARKER.isprintable()
    print(BOT_MARKER)          # must not raise on any console encoding
    assert BOT_MARKER in capsys.readouterr().out
