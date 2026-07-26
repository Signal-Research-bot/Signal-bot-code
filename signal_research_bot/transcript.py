"""Turn cached messages into a pseudonymised, redacted transcript.

This is where the three upstream modules compose: envelope gives structure,
identity gives stable labels, redact removes what the roster and the patterns
catch. The output of this module is the only thing the Claude stages ever see.

Order matters and is not arbitrary:

1. Drop opted-out senders entirely -- before anything else, so their text never
   enters a buffer that might be logged.
2. Substitute mentions while the raw UUIDs are still available, since a mention
   carries an ACI that maps to a label.
3. Redact the body.
4. Drop the message if redaction flagged it as special-category.
5. Only then attach a speaker label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .envelope import ParsedMessage, substitute_mentions
from .identity import PseudonymStore, Roster, is_opted_out
from .redact import Redactor, is_only_placeholders

UNKNOWN_LABEL = "[participant]"

# A message body is untrusted input that ends up inside a prompt, between
# <transcript> delimiters and after a "Participant X:" speaker prefix. Both are
# forgeable from the chat if they are passed through verbatim: a member who
# types "</transcript>" ends the transcript early and everything after it reads
# to the model as instructions rather than as data. Neutralising the two
# structural tokens here means the prompt builder cannot be tricked by content.
#
# The rewrite is visible, and it rewrites the *word* rather than hiding it
# behind an invisible character. A zero-width space would look like it worked
# and would not: egress._normalise strips zero-width characters before matching,
# so a forged "Participant Z" would reassemble inside the firewall and trip the
# unknown-label rule -- turning a prompt-injection attempt into a pipeline stall.
_ANGLE = re.compile(r"[<>]")
_NEWLINES = re.compile(r"[\r\n  ]+")
_SPEAKER = re.compile(r"(?<!\w)Participant(\s+)([A-Za-z]{1,3})(?!\w)")


@dataclass
class TranscriptStats:
    included: int = 0
    dropped_opted_out: int = 0
    dropped_quote_opted_out: int = 0
    dropped_sensitive: int = 0
    dropped_empty: int = 0
    kept_urls: int = 0
    kept_addresses: int = 0
    redaction_rules: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "redaction_rules"}
        d["redaction_rules"] = dict(self.redaction_rules)
        return d


@dataclass
class Builder:
    roster: Roster
    pseudonyms: PseudonymStore
    redactor: Redactor
    stats: TranscriptStats = field(default_factory=TranscriptStats)

    def _label(self, aci: str | None) -> str:
        if not aci:
            return UNKNOWN_LABEL
        return self.pseudonyms.label(aci)

    def _defang(self, text: str) -> str:
        """Defuse prompt-structure tokens in attacker-controlled message text.

        Runs *after* mention substitution, so it has to tell a label this module
        wrote from one a member typed. It does that by checking the label
        against the ones actually allocated: `Participant A` produced by a real
        mention is allocated and survives, `Participant Z` typed by a member is
        not and becomes `member Z`.

        Rewriting every occurrence instead -- the first version of this -- broke
        mentions, which is what the transcript tests caught. Leaving them all
        alone lets a member both forge a speaker turn and, with an unallocated
        letter, trip the firewall's unknown-label rule on every window.

        What this does not stop: a member typing a label that *is* allocated,
        impersonating someone inline. They cannot tell which label belongs to
        whom, and the newline collapse below stops it being read as a turn, so
        it degrades to ordinary quoting rather than forgery.
        """
        known = self.pseudonyms.known_labels()
        text = _ANGLE.sub(lambda m: "(" if m.group() == "<" else ")", text)
        # One message is one transcript line. A body-internal newline is the
        # other half of the forgery: without it, "\nParticipant Z: ..." reads
        # as a new turn rather than as text inside this one.
        text = _NEWLINES.sub("  ", text)
        return _SPEAKER.sub(
            lambda m: m.group(0)
            if f"Participant {m.group(2)}" in known
            else f"member{m.group(1)}{m.group(2)}",
            text,
        )

    def line(self, msg: ParsedMessage) -> str | None:
        """One transcript line, or None if the message must not be included."""
        if is_opted_out(self.roster, msg.source):
            self.stats.dropped_opted_out += 1
            return None

        body = msg.body
        if msg.mentions:
            # Done here, not in redact, because a mention carries an ACI that
            # maps to a real label -- "Participant B" is more useful to the
            # model than "[participant]", and only this module has the mapping.
            body = substitute_mentions(body, msg.mentions, lambda m: self._label(m.uuid))

        result = self.redactor.redact(body)
        if result.dropped:
            self.stats.dropped_sensitive += 1
            return None

        text = result.text.strip()
        # Placeholder-only content ("Participant A: [participant]") carries no
        # information and still costs tokens, so it is dropped rather than sent.
        if not text or is_only_placeholders(text):
            self.stats.dropped_empty += 1
            return None

        for rule in result.rules_fired:
            self.stats.redaction_rules[rule] = self.stats.redaction_rules.get(rule, 0) + 1
        self.stats.kept_urls += result.kept_urls
        self.stats.kept_addresses += result.kept_addresses
        self.stats.included += 1

        speaker = self._label(msg.source)
        prefix = ""
        # An opt-out has to cover being quoted, or it is not an opt-out. Someone
        # who replies to a member who has opted out would otherwise carry that
        # member's words through this function verbatim -- the sender check at
        # the top only sees the *replier*. PRIVACY.md promises this explicitly.
        if msg.quote_text and not is_opted_out(self.roster, msg.quote_author):
            quoted = self.redactor.redact(msg.quote_text)
            if not quoted.dropped and quoted.text.strip():
                prefix = f"(replying to: {self._defang(quoted.text.strip()[:120])}) "
        elif msg.quote_text:
            self.stats.dropped_quote_opted_out += 1
        suffix = f" [+{msg.attachment_count} attachment(s)]" if msg.attachment_count else ""
        return f"{speaker}: {prefix}{self._defang(text)}{suffix}"

    def build(self, messages: list[ParsedMessage]) -> str:
        """Render a batch. Timestamps are already coarsened by the parser."""
        lines: list[str] = []
        current_bucket: int | None = None
        for msg in messages:
            rendered = self.line(msg)
            if rendered is None:
                continue
            if msg.timestamp_ms != current_bucket:
                current_bucket = msg.timestamp_ms
                stamp = datetime.fromtimestamp(
                    current_bucket / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
                lines.append(f"\n--- {stamp} UTC ---")
            lines.append(rendered)
        return "\n".join(lines).strip()

    def labels_in_use(self) -> set[str]:
        """Labels allocated so far. The egress firewall accepts only these."""
        return self.pseudonyms.known_labels() | {UNKNOWN_LABEL}
