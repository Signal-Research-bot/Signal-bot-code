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
    "Quick heads up. I've switched on a bot that turns questions raised in "
    "here into a sourced research archive we can all read.\n"
    "\n"
    "It reads new messages, strips out what it can identify on my machine, "
    "then sends only the stripped text to an AI to research and answer with "
    "sources. Your name in the transcript becomes a label like \"Participant "
    "C\", and those labels are stripped out again before anything is written "
    "into the archive, so entries read as findings rather than as who said "
    "what.\n"
    "\n"
    "Being straight about the limit of that: phone numbers, emails and "
    "account IDs are removed by pattern, so they go whatever anyone types. "
    "Usernames are different -- it has to learn what you go by from watching "
    "the chat, so in the first few days, if someone types your username as "
    "ordinary text, it may not catch it. Tell me your username and I'll add "
    "it directly.\n"
    "\n"
    "How it works in practice:\n"
    "- Nothing changes for you. Just talk normally. There is no command, no "
    "trigger word, and nothing to address -- it reads the conversation and "
    "picks out what's worth checking on its own.\n"
    "- It looks for anything checkable, not just questions. An offhand claim "
    "like \"they took a stake through a subsidiary\" is exactly the kind of "
    "thing it will go and verify.\n"
    "- It only covers our actual subject: ownership and connections between "
    "companies and people in the space, reserves and attestations, filings, "
    "regulatory action, and claims of corruption or conflicts of interest. "
    "Everything else is ignored, including price talk and predictions.\n"
    "- It runs in batches, so answers show up later, not in the moment. It'll "
    "be quiet for the first while it settles in.\n"
    "- Every entry carries its sources, a confidence level, and what's still "
    "contested. Check the sources -- don't take the answer on trust.\n"
    "- I'll share access to the archive so you can all read it.\n"
    "\n"
    "Straight answers to the obvious questions:\n"
    # scrub-ok: privacy-word-overclaim
    "- It's pseudonymisation, not anonymisation. I hold the key that maps "
    "labels back to people, and it doesn't disguise how you write.\n"
    "- Disappearing messages are never processed at all.\n"
    "- Delete a message and it's dropped here too, and never researched. If "
    "it had already been written up before you deleted it, that entry stays "
    "until you tell me -- say the word and I'll pull it.\n"
    "- Opt out any time, just tell me. To skip a single message, put "
    "[research-bot] in it.\n"
    "- Bot messages appear under my name -- a linked device has no separate "
    "identity -- but they're tagged so you can tell.\n"
    "\n"
    "All the code is public if you want to check any of that: "
    "https://github.com/Signal-Research-bot/Signal-bot-code"
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

    def summarise_run(self, stats: dict[str, Any], entries: list[dict[str, Any]]) -> bool:
        """Post a run summary. Findings and headlines only -- never content."""
        text = format_summary(stats, entries)
        if text is None:
            return False
        return self.send(text)


# Ordered so the most interesting result is read first. A refutation is the
# thing people most want to know and the thing most likely to be missed if it
# is buried under a list of titles.
_FINDING_HEADINGS = [
    ("refuted", "❌ Not true"),
    ("mixed", "🟨 Partly true"),
    ("supported", "✅ Confirmed"),
    ("unestablished", "❓ Couldn't establish"),
]

SUMMARY_HEADER = "🔎 Research bot — new findings"

# One line, on every summary. A linked device has no separate identity, so the
# only place the automation disclosure can live is the message body itself.
SUMMARY_FOOTER = (
    "🤖 Automated message — compiled and posted by the research bot, "
    "not typed by hand."
)


def format_summary(
    stats: dict[str, Any], entries: list[dict[str, Any]]
) -> str | None:
    """Build the summary text, or None when there is nothing worth saying.

    Grouped by what was FOUND, not by what was worked on. A list of titles
    tells the group that effort happened; it does not tell them the Q1 report
    turned out not to be an audit. Each line leads with the result.

    A quiet window posts nothing. A bot that announces "0 new entries" every
    night trains everyone to ignore it, including on the night it matters.
    """
    written = stats.get("written", 0)
    deferred = stats.get("deferred_over_cap", 0)
    collided = stats.get("not_written_collision", 0)
    if not written and not deferred and not collided:
        return None

    body: list[str] = []
    by_finding: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        by_finding.setdefault(e.get("finding") or "unestablished", []).append(e)

    for key, heading in _FINDING_HEADINGS:
        group = by_finding.get(key) or []
        if not group:
            continue
        body.append(f"{heading}:")
        for e in group[:6]:
            # The headline carries the answer; the title is only a pointer to
            # the page, so it is deliberately not repeated here.
            text = (e.get("headline") or e.get("title") or "").strip()
            body.append(f"  - {text}")
        if len(group) > 6:
            body.append(f"  - ...and {len(group) - 6} more")
        body.append("")

    if not body:
        body = [f"{written} new " + ("entry" if written == 1 else "entries") + ".", ""]

    tail = []
    if deferred:
        # Surfaced deliberately. A silently truncated list reads exactly like
        # "nothing was missed", and the cap is the main cost lever. "Logged",
        # not "picked up next time": the pipeline never revisits these, and a
        # summary must not promise what the gate does not do.
        tail.append(
            f"{deferred} lead(s) deferred by the per-run cap; logged for manual follow-up."
        )
    if collided:
        # The research exists and the page does not. Says so plainly, and does
        # not promise a retry -- same discipline as the deferred line.
        tail.append(
            f"{collided} finding(s) could not be filed: an entry with the same "
            f"name already exists. Logged for manual follow-up; nothing was overwritten."
        )
    if stats.get("failed"):
        tail.append(f"{stats['failed']} task(s) failed and were skipped.")
    if tail:
        body.append(" ".join(tail))
        body.append("")

    return "\n".join([SUMMARY_HEADER, "", *body, SUMMARY_FOOTER]).strip()


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
