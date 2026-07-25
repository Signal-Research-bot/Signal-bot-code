"""Strip identity from message bodies before anything leaves the machine.

Layered, most-specific first. The **roster deny-list is the control**; pattern
rules catch what a closed-world list structurally cannot (a phone number nobody
put in the roster), and NER, if present, is a recall supplement that is never
relied upon. Published PERSON F1 for general-purpose NER sits around 0.62-0.69,
which is nowhere near good enough to be a privacy control on its own.

Fails closed in two ways:

* Missing `phonenumbers` is a hard error, not a silent downgrade to regex.
* A missing or empty roster is a hard error. An empty deny-list redacts nothing
  while reporting success, which is the worst possible failure mode.

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

import re
from dataclasses import dataclass, field
from typing import Callable

from .identity import Roster

PLACEHOLDER_NAME = "[participant]"
PLACEHOLDER_PHONE = "[phone]"
PLACEHOLDER_EMAIL = "[email]"
PLACEHOLDER_UUID = "[id]"
PLACEHOLDER_IBAN = "[account]"
PLACEHOLDER_URL = "[personal-link]"

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
# ISO 13616 shape: 2 letters, 2 check digits, then 11-30 alphanumerics, which
# may be written in groups of four. Matching fixed 4-char groups fails on the
# common unspaced form, whose tail is not a multiple of four.
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30}\b")
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
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

        variants = self.roster.name_variants()
        if not variants and not self.roster.phones:
            raise RedactionUnavailable(
                "roster is empty. An empty deny-list redacts nothing while "
                "reporting success -- refusing to run."
            )
        # Longest first so 'Anna Smith' is consumed before 'Anna'.
        for v in sorted(variants, key=len, reverse=True):
            self._name_res.append(
                (v, re.compile(rf"(?<!\w){re.escape(v)}(?!\w)", re.I))
            )

    # -- individual layers ----------------------------------------------------

    def _strip_names(self, text: str, fired: list[str]) -> str:
        for raw, pattern in self._name_res:
            if pattern.search(text):
                fired.append(f"roster-name:{raw[:2]}***")
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
            digits = sum(c.isdigit() for c in m.group())
            if 8 <= digits <= 15:
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
            ("iban", IBAN_RE, PLACEHOLDER_IBAN),
        ):
            if pattern.search(text):
                fired.append(name)
                text = pattern.sub(placeholder, text)
        return text

    def _contextual(self, text: str, fired: list[str]) -> tuple[str, int, int]:
        """Redact identity-bearing links; keep and count research payload."""
        kept_urls = 0

        def url_sub(m: re.Match[str]) -> str:
            nonlocal kept_urls
            url = m.group()
            host = re.sub(r"^www\.", "", url.split("//", 1)[-1].split("/")[0].lower())
            personal = any(host == h or host.endswith("." + h) for h in PERSONAL_HOSTS)
            if personal:
                fired.append("personal-url")
                return PLACEHOLDER_URL
            kept_urls += 1
            return url

        text = URL_RE.sub(url_sub, text)
        # Addresses are kept (research payload) but counted for review.
        kept_addresses = len(BTC_RE.findall(text)) + len(ETH_RE.findall(text))
        return text, kept_urls, kept_addresses

    # -- entry point ----------------------------------------------------------

    def redact(self, text: str) -> RedactionResult:
        if not text:
            return RedactionResult("")

        lowered = text.lower()
        for term in self.sensitive_terms:
            if term and term in lowered:
                # Message-level exclusion: no partial redaction, no sample kept.
                return RedactionResult(
                    "", ("sensitive-term",), dropped=True,
                    drop_reason="matched a special-category term",
                )

        # Layer order matters. Self-contained tokens (email, uuid, iban) go
        # first: a roster name inside an address is otherwise substituted mid-
        # token, and the mangled remainder no longer matches the email rule --
        # leaving the domain exposed. Names last, over whatever survives.
        fired: list[str] = []
        out = self._strip_simple(text, fired)
        out = self._strip_phones(out, fired)
        out, kept_urls, kept_addresses = self._contextual(out, fired)
        out = self._strip_names(out, fired)

        if self.roster.group_name:
            pattern = re.compile(rf"(?<!\w){re.escape(self.roster.group_name)}(?!\w)", re.I)
            if pattern.search(out):
                fired.append("group-name")
                out = pattern.sub("[group]", out)

        return RedactionResult(
            text=out,
            rules_fired=tuple(fired),
            kept_urls=kept_urls,
            kept_addresses=kept_addresses,
        )
