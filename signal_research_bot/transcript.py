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

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .envelope import ParsedMessage, substitute_mentions
from .identity import PseudonymStore, Roster, is_opted_out
from .redact import Redactor, is_only_placeholders

UNKNOWN_LABEL = "[participant]"


@dataclass
class TranscriptStats:
    included: int = 0
    dropped_opted_out: int = 0
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
        if msg.quote_text:
            quoted = self.redactor.redact(msg.quote_text)
            if not quoted.dropped and quoted.text.strip():
                prefix = f"(replying to: {quoted.text.strip()[:120]}) "
        suffix = f" [+{msg.attachment_count} attachment(s)]" if msg.attachment_count else ""
        return f"{speaker}: {prefix}{text}{suffix}"

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
