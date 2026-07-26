"""Strip identity from message bodies before anything leaves the machine.

Layered, most-specific first. The **roster deny-list is the control**; pattern
rules catch what a closed-world list structurally cannot (a phone number nobody
put in the roster), and NER, if present, is a recall supplement that is never
relied upon. Published PERSON F1 for general-purpose NER sits around 0.62-0.69,
which is nowhere near good enough to be a privacy control on its own.

Two failure modes, deliberately different:

* Missing `phonenumbers` is a hard error, not a silent downgrade to regex.
  `egress.py` imports `is_dialable` from this module, so losing the library
  would silently disarm a firewall rule as well as this one.
* An empty roster **warns and continues**. It used to be a hard error, on the
  reasoning that an empty deny-list redacts nothing while reporting success.
  That reasoning assumed members go by real names the operator could type in.
  In a group that is already pseudonymous there is nothing to type, so the
  hard error meant the tool never ran at all -- blocking everything while
  protecting nothing. The roster-independent shape rules in `egress.py` still
  apply, and `Roster.coverage()` is recorded per run so a bare roster cannot
  be mistaken for a covered one.

Real names and chat handles are enforced in different places, and that split is
load-bearing: see `Roster.handles`.

A judgement call worth knowing about
------------------------------------
This group researches crypto and finance. URLs and on-chain addresses are
usually the *substance* of the discussion, not identity -- a treasury address
or an SEC filing link is exactly what the pipeline exists to process. Blanket
redaction of those classes would gut the product while adding little privacy.

So they are handled contextually: an address or URL is redacted when it
co-occurs with roster identity or sits on a personal-profile host, and is
otherwise kept and *counted*, so a human can review the rate. Phones, emails,
IBANs, UUIDs and roster names have no such exemption -- they are always removed.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from .identity import Roster

log = logging.getLogger(__name__)

PLACEHOLDER_NAME = "[participant]"
PLACEHOLDER_PHONE = "[phone]"
PLACEHOLDER_EMAIL = "[email]"
PLACEHOLDER_UUID = "[id]"
PLACEHOLDER_IBAN = "[account]"
PLACEHOLDER_URL = "[personal-link]"

# A message whose content is nothing but placeholders carries no information.
# The transcript builder uses this to drop those lines rather than spend tokens
# on "Participant A: [participant]".
PLACEHOLDERS = frozenset(
    {
        PLACEHOLDER_NAME, PLACEHOLDER_PHONE, PLACEHOLDER_EMAIL,
        PLACEHOLDER_UUID, PLACEHOLDER_IBAN, PLACEHOLDER_URL, "[group]",
    }
)


def is_only_placeholders(text: str) -> bool:
    """True if nothing but placeholders and punctuation survived redaction."""
    stripped = text
    for placeholder in PLACEHOLDERS:
        stripped = stripped.replace(placeholder, " ")
    return not any(ch.isalnum() for ch in stripped)

# Hosts where a link is almost always to a person, not to a source document.
PERSONAL_HOSTS = frozenset(
    {
        "facebook.com", "instagram.com", "linkedin.com", "tiktok.com",
        "snapchat.com", "venmo.com", "cash.app", "paypal.me", "t.me",
        "wa.me", "signal.me", "strava.com", "untappd.com",
    }
)

EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+(?![\w.-])")
UUID_RE = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])",
    re.I,
)
# The same value with the dashes stripped -- how a UUID appears in a URL, or
# after passing through anything that removes punctuation. Both UUID rules
# required the dashes, so this form went through untouched.
#
# Deliberately NOT applied to bare hex generally: a 64-character SHA-256 and a
# 40-character Ethereum address are research payload in this archive, and the
# boundary assertions keep this to exactly 32.
UUID_COMPACT_RE = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{32}(?![0-9a-zA-Z])", re.I)
# ISO 13616 shape: 2 letters, 2 check digits, then 11-30 alphanumerics, which
# may be written in groups of four. Matching fixed 4-char groups fails on the
# common unspaced form, whose tail is not a multiple of four.
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30}\b")
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)

# The same personal hosts, written the way people actually paste them:
# "linkedin.com/in/someone", no scheme. URL_RE requires https?:// and never saw
# this form, so a profile link typed without a scheme went to Anthropic intact
# -- the one class of link PRIVACY.md promises is always removed.
#
# The mandatory `/path` is the whole discriminator. Matching the bare hostname
# would redact "I deleted my facebook.com account", which is prose about a
# person, not an identifier for one; requiring a path keeps that untouched while
# catching every form that actually resolves to somebody's profile.
#
# The lookbehind stops this re-matching inside something an earlier layer
# already decided about: a kept research URL whose path or query happens to
# contain a personal host, and the domain half of an address the email rule has
# already replaced.
_PERSONAL_HOST_ALT = "|".join(
    re.escape(h) for h in sorted(PERSONAL_HOSTS, key=len, reverse=True)
)
SCHEMELESS_PERSONAL_RE = re.compile(
    rf"(?<![\w@/.])(?:[\w-]+\.)*(?:{_PERSONAL_HOST_ALT})/[^\s<>\"')]+",
    re.I,
)

BTC_RE = re.compile(r"\b(?:bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
ETH_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

# Loose fallback for numbers phonenumbers will not parse without a region.
#
# Requires phone-*shaped* input: an international prefix, a national trunk
# zero, or separator-delimited groups. A bare run of digits is deliberately NOT
# matched -- in a finance chat "3200000000" is a reserves figure, and redacting
# it destroys the very content the pipeline exists to process. Over-redaction
# is not the safe side here; it is a different way of failing.
LOOSE_PHONE_RE = re.compile(
    r"(?<![\w+])(?:"
    r"\+\d[\d\s().-]{6,17}\d"                      # +1 415 555 0123
    r"|0\d[\d\s().-]{6,15}\d"                      # 07700 900123
    r"|\d{2,4}[\s().-]\d{2,4}[\s().-]\d{2,6}"      # 415-555-0123
    r")(?![\w])"
)

# A contiguous run of digits with no '+', no trunk zero and no separators --
# "whatsapp 447700900123". Every rule above requires one of those three, so this
# form passed redaction untouched, and it passed the firewall too because the
# E.164 rule requires the '+'.
#
# It cannot simply be redacted on shape: this pipeline exists to read a finance
# chat, where "127000000000" is a reserves figure and blanking it destroys the
# content the project is for. The discriminator is libphonenumber's
# `is_valid_number` against the run read as an international number, which
# checks it against real national numbering plans -- so a genuine mobile is
# caught and a balance sheet figure is not. Verified both ways in the tests.
BARE_DIGIT_RUN_RE = re.compile(r"(?<![\w+])(\d{10,15})(?![\w])")


# Characters that render as nothing, or that reorder what is rendered, and
# whose only use in a chat message is to break a pattern match. Kept in sync
# with egress._INVISIBLE -- see the note there on why bidi controls belong in
# this set even though they are not strictly invisible.
_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x034F, 0x180E]
    + list(range(0x200E, 0x2010))          # LRM, RLM, and the bidi marks
    + list(range(0x202A, 0x202F))          # LRE, RLE, PDF, LRO, RLO
    + list(range(0x2066, 0x206A))          # LRI, RLI, FSI, PDI
    + list(range(0xFFF9, 0xFFFC)),         # interlinear annotation
    None,
)


def fold_confusables(text: str) -> str:
    """Normalise text so a pattern match cannot be dodged by encoding.

    Two evasions this closes, both verified against the shipped rules:

    * **Non-ASCII decimal digits.** `str.isdigit()` is true for Arabic-Indic
      "٤٤٧..." and a dozen other scripts, but every phone rule in this module
      and in egress.py matches `\\d` against ASCII or strips with `[^0-9]`.
      NFKC does *not* fold them -- it folds fullwidth forms only. So a phone
      number typed in Eastern Arabic numerals passed every control. Digits are
      mapped through `unicodedata.digit()`, which knows all of them.
    * **Zero-width and bidi characters.** A soft hyphen inside a name defeats a
      substring match while rendering identically.

    Applied at the *start* of redaction rather than the end, so every layer
    downstream sees folded text.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    if folded.isascii():
        return folded
    out = []
    for ch in folded:
        if ch.isdigit() and not ch.isascii():
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(ch)
    return "".join(out)


def looks_like_phone(text: str) -> bool:
    """True if a phone-SHAPED candidate is actually a phone number.

    The shape rules alone are not safe in this archive. A SEC EDGAR accession
    number (`0001193125-24-206789`) and a CIK (`0000320193`) both match the
    separated-phone and trunk-zero shapes exactly, and those identifiers are
    the single most likely thing to appear in a chat about company filings.

    That was not a cosmetic false positive. The firewall blocked the window, and
    a blocked window is *consumed* rather than retried -- so one EDGAR link
    destroyed every message in that batch, permanently and silently. The two
    behaviours were each defensible alone and catastrophic together.

    Three checks, cheapest first:

    * More than 15 digits cannot be a phone number: E.164 caps the whole
      international number at 15. This alone rejects every accession number.
    * Read as an international number, is it assignable? Catches anything
      written with a country code.
    * Read as a national number in a plausible region, is it assignable? This
      is what keeps "020 7925 0918" caught, since it cannot be validated
      internationally without a region hint.
    """
    import phonenumbers  # noqa: PLC0415

    digits = re.sub(r"[^0-9]", "", text)
    if not 7 <= len(digits) <= 15:
        return False

    # A leading "+" is unambiguous: nothing in this archive's subject matter is
    # written that way, so the shape alone is enough and validity is not
    # consulted. This matters because the ranges reserved for fiction
    # (+44 7700 900xxx, +1 555) are deliberately NOT assignable, so a validity
    # check rejects exactly the numbers used in documentation and tests -- and,
    # more importantly, would let a real number through if libphonenumber's
    # tables happen to be behind on a new range.
    if text.lstrip().startswith("+"):
        return True

    if is_dialable(digits):
        return True
    for region in ("GB", "US"):
        try:
            if phonenumbers.is_valid_number(phonenumbers.parse(text, region)):
                return True
        except Exception:  # noqa: BLE001 - unparseable is not a phone number
            continue
    return False


def is_dialable(digits: str) -> bool:
    """True if a bare run of digits is a real, assignable phone number.

    Read as an international number and checked against libphonenumber's
    national numbering plans. This is deliberately *validity*, not
    *possibility*: `is_possible_number` only checks length for the country
    code, so it would call a ten-digit reserves figure beginning "32" a
    plausible Belgian number and redact it.

    Consequence worth stating plainly: numbers in the ranges reserved for
    fiction and documentation -- +44 7700 900xxx, some +1 555 -- are not valid,
    so they are not caught by this rule. They do not belong to anyone, which is
    why the trade is acceptable, but it does mean a test written with a "safe"
    example number will not exercise this path.
    """
    import phonenumbers  # noqa: PLC0415

    if not 10 <= len(digits) <= 15:
        return False
    try:
        return phonenumbers.is_valid_number(phonenumbers.parse("+" + digits, None))
    except Exception:  # noqa: BLE001 - unparseable is not a phone number
        return False


class RedactionUnavailable(RuntimeError):
    """A required redaction dependency or input is missing."""


@dataclass
class RedactionResult:
    text: str
    rules_fired: tuple[str, ...] = ()
    dropped: bool = False
    drop_reason: str | None = None
    # Counted-but-kept classes, for human review of the contextual policy.
    kept_urls: int = 0
    kept_addresses: int = 0
    # The kept URLs themselves, and not merely how many there were.
    #
    # This is ground truth, with one invariant that the rest of the system
    # relies on: **every string in here appears verbatim in `text`**. A URL that
    # a later layer rewrote -- a roster name inside its path, a personal host in
    # its query string -- is counted in `kept_urls` but is NOT listed here,
    # because handing it on whole would undo the redaction that shortened it.
    #
    # So this can legitimately be shorter than `kept_urls`. Anything that needs
    # a URL it may act on wants this list; anything reporting on the contextual
    # policy wants the count.
    kept_url_list: tuple[str, ...] = ()


@dataclass
class Redactor:
    roster: Roster
    label_for_aci: Callable[[str], str] | None = None
    # Message-level exclusion. The sensitive fact in a special-category message
    # is usually the whole message, so spans are the wrong unit -- drop it all.
    sensitive_terms: frozenset[str] = frozenset()
    _name_res: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        try:
            import phonenumbers  # noqa: F401, PLC0415
        except ImportError as exc:
            raise RedactionUnavailable(
                "phonenumbers is required. Refusing to run with regex-only "
                "phone detection, which would silently weaken redaction."
            ) from exc

        variants = self.roster.redaction_variants()
        if not variants and not self.roster.phones:
            # Degrades loudly; does NOT refuse.
            #
            # This used to raise. That was correct for a group whose members go
            # by their real names, and wrong for one that is already
            # pseudonymous: there is nothing for the operator to put in the
            # file, so the batch exited 2 on every window and the tool did not
            # run at all. A control that blocks everything while protecting
            # nothing is not the safe side of this trade.
            #
            # What survives an empty deny-list is not nothing. The egress
            # firewall's shape rules are roster-independent and fire in both
            # directions: E.164 and bare dialable numbers, emails, and UUIDs in
            # both dashed and compact form. Those cover the identifiers that
            # actually deanonymise a person. What is lost is names and handles
            # -- a real degradation, but a degradation and not a hole.
            #
            # Roster.coverage() is recorded per run so a window with nothing
            # configured cannot look identical to a fully covered one.
            log.warning(
                "roster has no names, handles or phones: name redaction is "
                "INACTIVE for this run. Identifier shape rules still apply. "
                "Add chat handles to var/roster.json to close this."
            )
        # Longest first so 'Anna Smith' is consumed before 'Anna'.
        for v in sorted(variants, key=len, reverse=True):
            self._name_res.append(
                (v, re.compile(rf"(?<!\w){re.escape(v)}(?!\w)", re.I))
            )

    # -- individual layers ----------------------------------------------------

    def _strip_names(self, text: str, fired: list[str]) -> str:
        # The rule name is written to metrics.jsonl and to logs. It used to
        # carry the first two characters of the matched name, which put real
        # member names -- partially, but recognisably in a group of eight --
        # into a file kept for cost analysis. The roster index is as useful for
        # tuning ("variant 3 never fires") and identifies nobody to a reader
        # who does not already hold the roster.
        for index, (_raw, pattern) in enumerate(self._name_res):
            if pattern.search(text):
                fired.append(f"roster-name:{index}")
                text = pattern.sub(PLACEHOLDER_NAME, text)
        return text

    def _strip_phones(self, text: str, fired: list[str]) -> str:
        import phonenumbers  # noqa: PLC0415

        spans: list[tuple[int, int]] = []
        for region in (None, "US", "GB"):
            try:
                for m in phonenumbers.PhoneNumberMatcher(text, region):
                    spans.append((m.start, m.end))
            except Exception:  # noqa: BLE001 - matcher is best-effort per region
                continue
        for m in LOOSE_PHONE_RE.finditer(text):
            # Shape is necessary but not sufficient. A CIK typed into chat has
            # the trunk-zero shape and is research payload, not a phone number;
            # turning "0000320193" into "[phone]" guts the archive's own subject
            # matter. looks_like_phone() decides.
            if looks_like_phone(m.group()):
                spans.append((m.start(), m.end()))

        for m in BARE_DIGIT_RUN_RE.finditer(text):
            if is_dialable(m.group(1)):
                spans.append((m.start(), m.end()))

        if not spans:
            return text
        fired.append("phone")
        for start, end in sorted(set(spans), reverse=True):
            text = text[:start] + PLACEHOLDER_PHONE + text[end:]
        return text

    def _strip_simple(self, text: str, fired: list[str]) -> str:
        for name, pattern, placeholder in (
            ("email", EMAIL_RE, PLACEHOLDER_EMAIL),
            ("uuid", UUID_RE, PLACEHOLDER_UUID),
            ("uuid-compact", UUID_COMPACT_RE, PLACEHOLDER_UUID),
            ("iban", IBAN_RE, PLACEHOLDER_IBAN),
        ):
            if pattern.search(text):
                fired.append(name)
                text = pattern.sub(placeholder, text)
        return text

    def _contextual(self, text: str, fired: list[str]) -> tuple[str, list[str], int]:
        """Redact identity-bearing links; keep and count research payload."""
        kept: list[str] = []

        def url_sub(m: re.Match[str]) -> str:
            url = m.group()
            host = re.sub(r"^www\.", "", url.split("//", 1)[-1].split("/")[0].lower())
            personal = any(host == h or host.endswith("." + h) for h in PERSONAL_HOSTS)
            if personal:
                fired.append("personal-url")
                return PLACEHOLDER_URL
            kept.append(url)
            return url

        text = URL_RE.sub(url_sub, text)
        # Runs after URL_RE, so every scheme-bearing link has already been
        # decided on and this pass only sees the bare form.
        if SCHEMELESS_PERSONAL_RE.search(text):
            fired.append("personal-url-bare")
            text = SCHEMELESS_PERSONAL_RE.sub(PLACEHOLDER_URL, text)
        # Addresses are kept (research payload) but counted for review.
        kept_addresses = len(BTC_RE.findall(text)) + len(ETH_RE.findall(text))
        return text, kept, kept_addresses

    # -- entry point ----------------------------------------------------------

    def redact(self, text: str) -> RedactionResult:
        if not text:
            return RedactionResult("")

        text = fold_confusables(text)
        lowered = text.lower()
        for term in self.sensitive_terms:
            if term and term in lowered:
                # Message-level exclusion: no partial redaction, no sample kept.
                return RedactionResult(
                    "", ("sensitive-term",), dropped=True,
                    drop_reason="matched a special-category term",
                )

        # Layer order matters, and every one of these orderings was got wrong
        # first and fixed after a test or an audit found the leak.
        #
        # Self-contained tokens (email, uuid, iban) go first: a roster name
        # inside an address is otherwise substituted mid-token, and the mangled
        # remainder no longer matches the email rule -- leaving the domain
        # exposed.
        #
        # The group name goes before names for the same reason one layer down.
        # A group called "Ravenhill Investors" whose roster contains "Ravenhill"
        # became "[participant] Investors" when names ran first: the group-name
        # pattern no longer matched, so the distinctive remainder survived and
        # went to Anthropic. Multi-token names are the common case, so this is
        # not an edge case.
        #
        # Names run last, over whatever survives.
        fired: list[str] = []
        out = self._strip_simple(text, fired)
        out = self._strip_phones(out, fired)
        out, kept, kept_addresses = self._contextual(out, fired)

        if self.roster.group_name:
            pattern = re.compile(rf"(?<!\w){re.escape(self.roster.group_name)}(?!\w)", re.I)
            if pattern.search(out):
                fired.append("group-name")
                out = pattern.sub("[group]", out)

        out = self._strip_names(out, fired)

        return RedactionResult(
            text=out,
            rules_fired=tuple(fired),
            kept_urls=len(kept),
            kept_addresses=kept_addresses,
            # Filtered against the FINAL text, not against what _contextual saw.
            # The group-name and roster-name passes run after it and will happily
            # rewrite the middle of a URL, so a URL that was kept there is not
            # necessarily a URL that survived to here. See kept_url_list.
            kept_url_list=tuple(u for u in kept if u in out),
        )
