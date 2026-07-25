"""Post the bot's announcement and run summaries back into the Signal group.

Two things about sending that are easy to get wrong and hard to undo:

* **A linked device has no separate bot identity.** Anything sent here appears
  in the group as a message from the operator, in their own name, on all their
  devices. There is no "this is a bot" badge. The announcement text says so
  explicitly, because members will otherwise reasonably read it as the operator
  typing.
* **Whatever is sent comes straight back in through the receiver.** Every
  message carries BOT_MARKER, and the receiver drops anything containing it.
  Without that the next window ingests the previous window's summary and
  summarises the summary, forever.

Summaries carry counts and entry titles only. Never a transcript excerpt, never
a participant label, never a question verbatim from chat -- a summary is the
one thing here that is deliberately broadcast, so it holds the least.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from typing import Any

from .receiver import BOT_MARKER

log = logging.getLogger(__name__)

SEND_TIMEOUT = 30.0

# Built by concatenation rather than one triple-quoted block, so the scrub-ok
# pragma can sit on a *code* line. Inside a triple-quoted string the pragma
# would become part of the message members actually receive.
#
# The banned word is used deliberately here and is worth the pragma: naming the
# distinction is the entire point of the sentence, and "it is pseudonymisation,
# not the stronger claim" tells a non-technical reader nothing.
ANNOUNCEMENT = (
    "Heads up: I've switched on a research bot for this group.\n"
    "\n"
    "What it does: it reads new messages, strips out names and numbers locally, "
    "and sends the stripped text to an AI to pull out research questions and "
    "answer them with sources. Answers go into a private archive only people in "
    "this group can read.\n"
    "\n"
    "What you should know:\n"
    # scrub-ok: privacy-word-overclaim
    "- It is pseudonymisation, not anonymisation. I hold the key that maps "
    "labels back to people, and writing style is not disguised.\n"
    "- Disappearing messages are never processed. Deleted and edited messages "
    "are removed or superseded here too.\n"
    "- You can opt out entirely, any time. Just tell me and your messages are "
    "dropped at collection. You can also drop a single message by typing "
    "[research-bot] anywhere in it.\n"
    "- The full source code is public so you can check any of this yourself.\n"
    "\n"
    "Messages from the bot will appear under my name, because it runs as a "
    "linked device on my account. There is no way to make it show up as anyone "
    "else."
)


class SendFailed(RuntimeError):
    """The message could not be delivered."""


@dataclass
class Notifier:
    host: str
    port: int
    group_id: str
    enabled: bool = True

    def _call(self, frame: dict[str, Any]) -> dict[str, Any]:
        payload = (json.dumps(frame) + "\n").encode("utf-8")
        try:
            with socket.create_connection((self.host, self.port), timeout=SEND_TIMEOUT) as sock:
                sock.settimeout(SEND_TIMEOUT)
                sock.sendall(payload)
                buf = b""
                while b"\n" not in buf:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as exc:
            raise SendFailed(f"could not reach signal-cli: {type(exc).__name__}") from exc

        line = buf.split(b"\n", 1)[0].strip()
        if not line:
            raise SendFailed("signal-cli closed the connection without responding")
        response = json.loads(line.decode("utf-8", errors="replace"))
        if "error" in response:
            # Never log the frame: it would echo the message body into the log.
            raise SendFailed(f"signal-cli rejected the send: {response['error'].get('code')}")
        return response

    def send(self, text: str) -> bool:
        """Send one message, marked so the receiver will not re-ingest it."""
        if not self.enabled:
            log.info("notifications disabled; not sending")
            return False
        frame = {
            "jsonrpc": "2.0",
            "id": "notify",
            "method": "send",
            "params": {"groupId": self.group_id, "message": f"{text}\n{BOT_MARKER}"},
        }
        self._call(frame)
        log.info("notification sent", extra={"chars": len(text)})
        return True

    def announce(self) -> bool:
        return self.send(ANNOUNCEMENT)

    def summarise_run(self, stats: dict[str, Any], titles: list[str]) -> bool:
        """Post a run summary. Counts and titles only."""
        text = format_summary(stats, titles)
        if text is None:
            return False
        return self.send(text)


def format_summary(stats: dict[str, Any], titles: list[str]) -> str | None:
    """Build the summary text, or None when there is nothing worth saying.

    A quiet window posts nothing. A bot that announces "0 new entries" every
    night trains everyone to ignore it, including on the night it matters.
    """
    written = stats.get("written", 0)
    deferred = stats.get("deferred_over_cap", 0)
    if not written and not deferred:
        return None

    lines = [f"Research update: {written} new " + ("entry" if written == 1 else "entries") + "."]
    for title in titles[:10]:
        lines.append(f"  - {title}")
    if len(titles) > 10:
        lines.append(f"  - ...and {len(titles) - 10} more")

    if deferred:
        # Surfaced deliberately. A silently truncated list reads exactly like
        # "nothing was missed", and the cap is the main cost lever.
        lines.append(
            f"\n{deferred} question(s) were deferred by the per-run cap and "
            f"will be picked up next time."
        )
    if stats.get("failed"):
        lines.append(f"{stats['failed']} task(s) failed and were skipped.")
    return "\n".join(lines)


def main() -> int:
    """CLI: post the announcement, or a test message, to the group.

        python -m signal_research_bot.notify --announce
        python -m signal_research_bot.notify --test
    """
    import argparse
    import sys

    # A Windows console defaults to cp1252 and raises on anything outside it.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from .config import Config, ConfigError
    from .logging_setup import configure

    parser = argparse.ArgumentParser(description="Send a message to the group.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--announce", action="store_true",
                       help="post the one-time announcement explaining the bot")
    group.add_argument("--test", action="store_true",
                       help="post a short message to confirm sending works")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent, send nothing")
    args = parser.parse_args()

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure(cfg.log_level)

    text = ANNOUNCEMENT if args.announce else "Test message from the research bot setup."
    if args.dry_run:
        print(text + "\n" + BOT_MARKER)
        print("\n-- dry run: nothing was sent --")
        return 0

    try:
        Notifier(cfg.signal_host, cfg.signal_port, cfg.group_id).send(text)
    except SendFailed as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return 1
    print("sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
