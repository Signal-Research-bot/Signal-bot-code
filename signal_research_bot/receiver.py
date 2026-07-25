"""Line-delimited JSON-RPC 2.0 client for signal-cli, over TCP.

Not HTTP, and deliberately not `bbernhard/signal-cli-rest-api`. That wrapper
fans received messages out over an unbuffered Go channel with a non-blocking
`default:` -- a slow or absent consumer drops messages that signal-cli has
already acknowledged to Signal's servers, permanently and without an error.
`RECEIVE_WEBHOOK_URL` does not fix it: the fan-out runs first, webhook failures
are logged and the loop continues, with no retry and no queue.

The durability of this whole system rests on one flag on the daemon:

    signal-cli --config /data daemon --tcp 0.0.0.0:7583 --receive-mode=on-connection

`on-connection` means signal-cli fetches from Signal only while a JSON-RPC
client is attached. When this process is down, the queue stays server-side and
is delivered on reconnect. Under any other mode, messages are acknowledged to
Signal before this process sees them, and a crash loses them silently.

Consequences for this file:

* The TCP connection *is* the subscription. There is no subscribe call to make
  (that is `--receive-mode=manual`), and dropping the connection is the correct
  way to apply backpressure.
* Reconnect must be robust and must never give up. Docker Desktop restarts on
  update, WSL restarts, and the host sleeps -- all of which drop the socket.
* Writing to the cache is the *first* thing done with an inbound message,
  before any parsing that could raise.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .cache import Cache
from .envelope import DisappearingMessage, ParsedMessage, parse

log = logging.getLogger(__name__)

# Marker the bot puts in its own summary posts. Those arrive back through the
# receiver like any other message; without this the next window ingests the
# previous window's summary and summarises the summary, forever.
BOT_MARKER = "⁣[research-bot]"

BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0
READ_TIMEOUT = 300.0        # signal-cli is quiet between messages; this is not idle-kill
STATE_LAST_SEEN = "last_seen_ms"


@dataclass
class ReceiverStats:
    connects: int = 0
    envelopes: int = 0
    stored: int = 0
    duplicates: int = 0
    dropped_expiring: int = 0
    dropped_bot_echo: int = 0
    other_group: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def read_lines(sock: socket.socket) -> Iterator[str]:
    """Yield complete newline-delimited frames from a socket.

    signal-cli emits one JSON object per line, but TCP does not preserve those
    boundaries -- a read can return half a frame, or three of them. Buffering
    until a newline is the only correct way to parse this.
    """
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return                      # peer closed
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line:
                yield line.decode("utf-8", errors="replace")


def envelope_of(frame: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the envelope from a JSON-RPC notification, if it is one."""
    if frame.get("method") != "receive":
        return None                     # a response to a call we made, or a ping
    params = frame.get("params") or {}
    return params.get("envelope") or None


@dataclass
class Receiver:
    host: str
    port: int
    group_id: str
    cache: Cache
    stats: ReceiverStats = field(default_factory=ReceiverStats)
    _stop: bool = False

    def stop(self) -> None:
        self._stop = True

    # -- one connection ------------------------------------------------------

    def _handle(self, frame: dict[str, Any]) -> None:
        env = envelope_of(frame)
        if env is None:
            return
        self.stats.envelopes += 1

        try:
            msg = parse(env, self.group_id)
        except DisappearingMessage:
            # Never cached, never counted beyond a tally. The sender configured
            # this content to vanish; persisting it would contradict that.
            self.stats.dropped_expiring += 1
            return

        if msg is None:
            self.stats.other_group += 1
            return

        if BOT_MARKER in msg.body:
            self.stats.dropped_bot_echo += 1
            return

        if self.cache.apply(msg):
            self.stats.stored += 1
        else:
            self.stats.duplicates += 1

        # Liveness, for the linked-device expiry probe. A device unlinks after
        # a long enough gap and re-linking needs a human with the phone, so a
        # stale value here is the signal that something needs attention.
        self.cache.set_state(STATE_LAST_SEEN, str(msg.raw_timestamp_ms))

    def run_once(self, on_frame: Callable[[dict], None] | None = None) -> None:
        """Connect, consume until the peer closes, then return."""
        with socket.create_connection((self.host, self.port), timeout=30) as sock:
            sock.settimeout(READ_TIMEOUT)
            self.stats.connects += 1
            log.info("receiver connected", extra={"host": self.host, "port": self.port})
            for line in read_lines(sock):
                if self._stop:
                    return
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    # Never log the frame: it may contain message content.
                    log.warning("discarding unparseable frame", extra={"bytes": len(line)})
                    continue
                (on_frame or self._handle)(frame)

    # -- forever -------------------------------------------------------------

    def run_forever(self, sleep: Callable[[float], None] = time.sleep) -> None:
        """Reconnect indefinitely with exponential backoff and jitter.

        Never gives up. The alternative -- exiting after N failures -- means a
        transient Docker restart silently ends collection, and nothing fetches
        from Signal until a human notices.
        """
        backoff = BACKOFF_INITIAL
        while not self._stop:
            try:
                self.run_once()
                backoff = BACKOFF_INITIAL     # clean disconnect: reconnect promptly
            except (OSError, socket.timeout) as exc:
                log.warning(
                    "receiver connection failed; retrying",
                    extra={"error": type(exc).__name__, "backoff_s": round(backoff, 1)},
                )
            if self._stop:
                return
            # Jitter so a restarted stack does not reconnect in lockstep.
            sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, BACKOFF_MAX)


def build_send_frame(group_id: str, text: str, request_id: str = "1") -> dict[str, Any]:
    """A JSON-RPC send call, with the bot marker attached.

    The marker matters: a linked device has no separate bot identity, so this
    message appears in the group as one from the operator, and it arrives back
    through the receiver like any other message.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "send",
        "params": {"groupId": group_id, "message": f"{text}\n{BOT_MARKER}"},
    }
