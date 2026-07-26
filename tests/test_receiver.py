"""Tests for the JSON-RPC receiver.

Driven against a real in-process TCP server, not a mock, because the bugs worth
catching here are transport bugs: frame boundaries, reconnection, and the
message-loss behaviour that made us reject the REST wrapper in the first place.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.cache import Cache  # noqa: E402
from signal_research_bot.identity import Roster  # noqa: E402
from signal_research_bot.receiver import (  # noqa: E402
    BOT_MARKER,
    Receiver,
    build_send_frame,
    envelope_of,
    read_lines,
)

GROUP = "Zm9vYmFyZ3JvdXBpZA=="
ALICE = str(uuid.UUID(int=0xA11CE))
BOB = str(uuid.UUID(int=0xB0B))
TS = 1_784_000_000_000


def notification(body: str = "hello", ts: int = TS, sync: bool = False,
                 source: str = ALICE) -> dict:
    payload = {"groupInfo": {"groupId": GROUP}, "message": body}
    env = {"sourceUuid": source, "timestamp": ts}
    env["syncMessage"] = {"sentMessage": payload} if sync else None
    if sync:
        env.pop("dataMessage", None)
    else:
        env.pop("syncMessage")
        env["dataMessage"] = payload
    return {"jsonrpc": "2.0", "method": "receive", "params": {"envelope": env}}


class FakeDaemon:
    """A TCP server that writes caller-supplied bytes, then closes."""

    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            with conn:
                for chunk in self.chunks:
                    try:
                        conn.sendall(chunk)
                    except OSError:
                        break

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def cache(tmp_path):
    c = Cache.open(tmp_path / "c.db", allow_plaintext=True)
    yield c
    c.close()


def drain(cache, chunks: list[bytes]) -> Receiver:
    daemon = FakeDaemon(chunks)
    try:
        r = Receiver("127.0.0.1", daemon.port, GROUP, cache)
        r.run_once()
        return r
    finally:
        daemon.close()


# --- framing -----------------------------------------------------------------


def test_message_split_across_tcp_reads_is_reassembled(cache):
    """TCP does not preserve line boundaries; a naive recv-and-parse loses this."""
    line = json.dumps(notification()).encode() + b"\n"
    half = len(line) // 2
    r = drain(cache, [line[:half], line[half:]])
    assert r.stats.stored == 1


def test_multiple_messages_in_one_read_are_all_parsed(cache):
    lines = b"".join(json.dumps(notification(ts=TS + i)).encode() + b"\n" for i in range(3))
    r = drain(cache, [lines])
    assert r.stats.stored == 3


def test_blank_lines_and_keepalives_are_ignored(cache):
    payload = b"\n\n" + json.dumps(notification()).encode() + b"\n\n"
    r = drain(cache, [payload])
    assert r.stats.stored == 1


def test_unparseable_frame_does_not_kill_the_connection(cache):
    payload = b"{not json\n" + json.dumps(notification()).encode() + b"\n"
    r = drain(cache, [payload])
    assert r.stats.stored == 1


def test_read_lines_stops_cleanly_on_peer_close():
    daemon = FakeDaemon([b"one\n"])
    try:
        with socket.create_connection(("127.0.0.1", daemon.port)) as s:
            assert list(read_lines(s)) == ["one"]
    finally:
        daemon.close()


# --- routing -----------------------------------------------------------------


def test_non_receive_frames_are_ignored(cache):
    resp = json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"timestamp": 1}}).encode()
    r = drain(cache, [resp + b"\n"])
    assert r.stats.envelopes == 0 and r.stats.stored == 0


def test_envelope_of_ignores_call_responses():
    assert envelope_of({"jsonrpc": "2.0", "id": "1", "result": {}}) is None


def test_own_message_via_syncmessage_is_stored(cache):
    """The linked-device trap, end to end through the transport."""
    r = drain(cache, [json.dumps(notification(sync=True)).encode() + b"\n"])
    assert r.stats.stored == 1


def test_message_for_another_group_is_counted_not_stored(cache):
    frame = notification()
    frame["params"]["envelope"]["dataMessage"]["groupInfo"]["groupId"] = "b3RoZXI="
    r = drain(cache, [json.dumps(frame).encode() + b"\n"])
    assert r.stats.other_group == 1 and r.stats.stored == 0


def test_disappearing_message_is_dropped_not_cached(cache):
    frame = notification()
    frame["params"]["envelope"]["dataMessage"]["expiresInSeconds"] = 86400
    r = drain(cache, [json.dumps(frame).encode() + b"\n"])
    assert r.stats.dropped_expiring == 1
    assert cache.counts()["total"] == 0


def test_bot_own_summary_is_not_reingested(cache):
    """Without this the next window summarises the previous window's summary."""
    frame = notification(body=f"Processed 12 messages\n{BOT_MARKER}")
    r = drain(cache, [json.dumps(frame).encode() + b"\n"])
    assert r.stats.dropped_bot_echo == 1 and cache.counts()["total"] == 0


def test_duplicate_delivery_is_counted_separately(cache):
    line = json.dumps(notification()).encode() + b"\n"
    r = drain(cache, [line + line])
    assert r.stats.stored == 1 and r.stats.duplicates == 1


def test_last_seen_is_recorded_for_the_liveness_probe(cache):
    drain(cache, [json.dumps(notification()).encode() + b"\n"])
    assert cache.get_state("last_seen_ms") == str(TS)


# --- reconnection ------------------------------------------------------------


def test_run_forever_reconnects_after_the_peer_closes(cache):
    """Docker restarts, WSL restarts and host sleep all drop the socket."""
    daemon = FakeDaemon([json.dumps(notification()).encode() + b"\n"])
    try:
        r = Receiver("127.0.0.1", daemon.port, GROUP, cache)
        calls = {"n": 0}

        def fake_sleep(_seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] >= 3:
                r.stop()

        r.run_forever(sleep=fake_sleep)
        assert daemon.connections >= 2, "receiver did not reconnect"
    finally:
        daemon.close()


def test_run_forever_survives_a_refused_connection(cache):
    """A dead daemon must not end collection."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()                      # nothing is listening here

    r = Receiver("127.0.0.1", port, GROUP, cache)
    attempts = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        attempts["n"] += 1
        if attempts["n"] >= 3:
            r.stop()

    r.run_forever(sleep=fake_sleep)   # must return, not raise
    assert attempts["n"] >= 3


def test_backoff_grows_and_is_jittered(cache):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    r = Receiver("127.0.0.1", port, GROUP, cache)
    waits: list[float] = []

    def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 4:
            r.stop()

    r.run_forever(sleep=fake_sleep)
    assert waits[-1] > waits[0], "backoff did not grow"
    assert len(set(waits)) > 1, "backoff is not jittered"


# --- sending -----------------------------------------------------------------


def test_send_frame_carries_the_bot_marker():
    frame = build_send_frame(GROUP, "Processed 12 messages")
    assert frame["method"] == "send"
    assert BOT_MARKER in frame["params"]["message"]
    assert frame["params"]["groupId"] == GROUP


def test_an_opted_out_member_is_never_written_to_the_cache(tmp_path):
    """PRIVACY.md says an opt-out drops your messages "at the point of
    collection". It was enforced only at transcript-build time, so the messages
    were in fact collected, decrypted-at-rest on the operator's disk, and merely
    filtered later. The sentence is the promise; this makes it literal."""
    cache = Cache.open(tmp_path / "c.db", None, allow_plaintext=True)
    roster = Roster(names=("Anna Smith",), phones=(), opted_out=frozenset({ALICE}))
    r = Receiver("h", 1, GROUP, cache, roster)

    r._handle(notification("something they said", source=ALICE))
    assert r.stats.dropped_opted_out == 1
    assert cache.pending() == []

    r._handle(notification("something else", ts=TS + 60_000, source=BOB))
    assert len(cache.pending()) == 1
    cache.close()


def test_without_a_roster_the_receiver_still_collects(tmp_path):
    """A missing roster must not stop collection -- losing messages is the one
    failure this component exists to prevent."""
    cache = Cache.open(tmp_path / "c.db", None, allow_plaintext=True)
    r = Receiver("h", 1, GROUP, cache, None)
    r._handle(notification("hello", source=ALICE))
    assert len(cache.pending()) == 1
    cache.close()


def test_one_poison_frame_does_not_kill_the_receiver(tmp_path):
    """run_forever caught only OSError and socket.timeout, so anything raised
    while HANDLING a message escaped and killed the process. The message was
    never acknowledged to Signal, so the restart redelivered it and the process
    died again -- one crafted message could stop all collection indefinitely."""
    cache = Cache.open(tmp_path / "c.db", None, allow_plaintext=True)
    r = Receiver("h", 1, GROUP, cache)
    calls = {"n": 0}

    def exploding(_frame):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("crafted envelope")
        r._handle(_frame)

    good = json.dumps(notification("survived", ts=TS + 60_000)).encode()
    bad = json.dumps(notification("poison")).encode()
    daemon = FakeDaemon([bad + b"\n" + good + b"\n"])
    try:
        r.host, r.port = "127.0.0.1", daemon.port
        r.run_once(on_frame=exploding)
    finally:
        daemon.close()

    assert r.stats.errors == 1
    assert calls["n"] == 2, "the loop stopped at the poison frame"
    assert len(cache.pending()) == 1, "the message after the poison one was lost"
    cache.close()


def test_the_opt_out_marker_is_case_insensitive(tmp_path):
    """The marker is documented to members as a per-message opt-out. Someone
    typing "[Research-Bot]" plainly meant to use it; an exact-case match
    silently ingested the message they were trying to keep out."""
    cache = Cache.open(tmp_path / "c.db", None, allow_plaintext=True)
    r = Receiver("h", 1, GROUP, cache)
    r._handle(notification("[Research-Bot] please skip this"))
    assert r.stats.dropped_bot_echo == 1
    assert cache.pending() == []
    cache.close()


def test_display_names_are_recorded_for_the_operator(tmp_path):
    """The answer to "how do I fill in the roster for a group whose members I
    cannot name". Signal attaches sourceName to envelopes the receiver is
    already holding a connection for, so this costs no extra RPC call."""
    cache = Cache.open(tmp_path / "c.db", None, allow_plaintext=True)
    out = tmp_path / "observed-handles.json"
    r = Receiver("h", 1, GROUP, cache, None, observed_handles_path=out)

    frame = notification("hello")
    frame["params"]["envelope"]["sourceName"] = "zeropoint_x"
    r._handle(frame)

    recorded = json.loads(out.read_text(encoding="utf-8"))
    assert recorded["observed"] == ["zeropoint_x"]
    cache.close()


def test_observed_handles_never_record_a_name_to_aci_mapping(tmp_path):
    """The list is all that is needed to populate `handles`; the mapping would
    be a strictly more sensitive file for no extra benefit."""
    cache = Cache.open(tmp_path / "c.db", None, allow_plaintext=True)
    out = tmp_path / "observed-handles.json"
    r = Receiver("h", 1, GROUP, cache, None, observed_handles_path=out)

    frame = notification("hello")
    frame["params"]["envelope"]["sourceName"] = "zeropoint_x"
    r._handle(frame)

    assert ALICE not in out.read_text(encoding="utf-8")
    cache.close()


def test_observing_handles_is_off_unless_a_path_is_given(tmp_path):
    cache = Cache.open(tmp_path / "c.db", None, allow_plaintext=True)
    r = Receiver("h", 1, GROUP, cache)
    frame = notification("hello")
    frame["params"]["envelope"]["sourceName"] = "zeropoint_x"
    r._handle(frame)          # must not raise
    assert len(cache.pending()) == 1
    cache.close()
