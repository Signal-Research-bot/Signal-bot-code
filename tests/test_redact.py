"""Tests for the redaction layers.

Every fixture is SYNTHETIC. Phone numbers and UUIDs are assembled from parts
rather than written as literals -- see test_envelope.py for the reasoning: a
tracked file should never contain a contiguous phone- or UUID-shaped string,
because neither scrub_check nor a reviewer can tell a fake one from a real one.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.identity import Roster  # noqa: E402
from signal_research_bot.redact import (  # noqa: E402
    PLACEHOLDER_EMAIL,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PHONE,
    PLACEHOLDER_URL,
    Redactor,
    RedactionUnavailable,
)

# Assembled at runtime, never written as contiguous literals. The repo holds a
# simple, auditable invariant: no tracked file contains a phone-, email- or
# UUID-shaped string, so scrub_check has no legitimate reason to ever see one.
US_PHONE = "+" + "1" + "415" + "555" + "0123"       # NANP 555-01xx, reserved
UK_PHONE = "+" + "44" + "7700" + "900" + "123"      # Ofcom drama range
FAKE_UUID = str(uuid.UUID(int=0xDECAF))
FAKE_LOCAL = "a.smith"
FAKE_DOMAIN = "example" + ".invalid"                # RFC 2606: never routable
FAKE_EMAIL = FAKE_LOCAL + "@" + FAKE_DOMAIN


@pytest.fixture
def roster():
    return Roster(
        names=("Anna Smith", "Bo"),
        phones=(US_PHONE,),
        group_name="Ravenhill",
    )


@pytest.fixture
def redactor(roster):
    return Redactor(roster=roster)


# --- fail-closed -------------------------------------------------------------


def test_empty_roster_refuses_to_run():
    """An empty deny-list redacts nothing while reporting success."""
    with pytest.raises(RedactionUnavailable):
        Redactor(roster=Roster())


def test_missing_roster_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Roster.load(tmp_path / "absent.json")


# --- roster names are the control -------------------------------------------


def test_full_name_removed(redactor):
    assert "Anna" not in redactor.redact("Anna Smith said so").text


def test_first_name_alone_removed(redactor):
    assert redactor.redact("ask Anna").text == f"ask {PLACEHOLDER_NAME}"


def test_possessive_removed(redactor):
    """Presidio's deny_list has no notion of possessives; ours must."""
    assert "Anna" not in redactor.redact("that was Anna's point").text


def test_case_insensitive(redactor):
    assert "ANNA" not in redactor.redact("ANNA disagreed").text.upper().replace(
        PLACEHOLDER_NAME.upper(), ""
    )


def test_longest_name_wins(redactor):
    """'Anna Smith' must be consumed whole, not left as '[participant] Smith'."""
    assert redactor.redact("Anna Smith").text == PLACEHOLDER_NAME


def test_short_name_does_not_eat_substrings(redactor):
    """'Bo' must not fire inside 'Bolivia' -- a noisy redactor gets disabled."""
    assert redactor.redact("Bolivia devalued").text == "Bolivia devalued"


def test_group_name_removed(redactor):
    assert "Ravenhill" not in redactor.redact("the Ravenhill crew").text


# --- pattern layers ----------------------------------------------------------


@pytest.mark.parametrize("phone", [US_PHONE, UK_PHONE])
def test_phone_numbers_removed(redactor, phone):
    out = redactor.redact(f"call me on {phone} later").text
    assert PLACEHOLDER_PHONE in out and phone not in out


def test_phone_not_in_roster_is_still_removed(redactor):
    """Pattern layers exist to catch what a closed-world list cannot."""
    stranger = "+" + "1" + "212" + "555" + "0198"
    assert stranger not in redactor.redact(f"try {stranger}").text


def test_email_removed(redactor):
    out = redactor.redact(f"mail {FAKE_EMAIL} now").text
    assert PLACEHOLDER_EMAIL in out and FAKE_DOMAIN not in out


def test_uuid_removed(redactor):
    assert FAKE_UUID not in redactor.redact(f"aci {FAKE_UUID}").text


def test_iban_removed(redactor):
    iban = "GB29" + "NWBK" + "6016" + "1331" + "9268" + "19"
    assert iban not in redactor.redact(f"send to {iban}").text


@pytest.mark.parametrize(
    "text",
    [
        "reserves hit 3200000000 usd",     # bare digit run
        "filed in 2024 under rule 10b5-1",
        "the 2026 10-K restated 1450000",
        "block 840000 halved the subsidy",
    ],
)
def test_figures_are_not_mistaken_for_phone_numbers(redactor, text):
    """Regression: a bare digit run is a figure, not a phone.

    Over-redaction is not the safe side here -- it destroys exactly the content
    the pipeline exists to process. The loose matcher requires phone *shape*
    (international prefix, trunk zero, or separators), not merely length.
    """
    assert redactor.redact(text).text == text


def test_name_inside_an_email_does_not_expose_the_domain(redactor):
    """Regression: layer ordering.

    Names used to run first, so a roster surname inside the local part was
    substituted mid-token. The mangled remainder no longer matched the email
    rule, and the domain survived. Self-contained tokens go first.
    """
    out = redactor.redact(f"write to {FAKE_EMAIL}").text
    assert FAKE_DOMAIN not in out and "smith" not in out.lower()


# --- contextual policy: research payload survives ---------------------------


def test_research_url_is_kept(redactor):
    url = "https://www.sec.gov/Archives/edgar/data/123/filing.htm"
    result = redactor.redact(f"see {url}")
    assert url in result.text and result.kept_urls == 1


def test_personal_profile_url_is_redacted(redactor):
    result = redactor.redact("https://www.linkedin.com/in/someone-real")
    assert result.text == PLACEHOLDER_URL and result.kept_urls == 0


def test_messaging_deeplink_is_redacted(redactor):
    assert PLACEHOLDER_URL in redactor.redact("ping https://t.me/handle").text


def test_crypto_address_is_kept_but_counted(redactor):
    """In a crypto research group an address is usually the subject, not identity."""
    addr = "0x" + "a" * 40
    result = redactor.redact(f"treasury {addr}")
    assert addr in result.text and result.kept_addresses == 1


# --- message-level exclusion -------------------------------------------------


def test_sensitive_term_drops_whole_message(roster):
    """For special-category content the sensitive fact is usually the whole
    message, so spans are the wrong unit."""
    r = Redactor(roster=roster, sensitive_terms=frozenset({"diagnosis"}))
    result = r.redact("got my diagnosis back, not great")
    assert result.dropped and result.text == ""
    assert result.drop_reason


def test_dropped_message_leaks_no_sample(roster):
    r = Redactor(roster=roster, sensitive_terms=frozenset({"diagnosis"}))
    result = r.redact("diagnosis: something private")
    assert "something private" not in repr(result)


# --- reporting ---------------------------------------------------------------


def test_rules_fired_do_not_echo_the_matched_name(redactor):
    """Telemetry names the rule, never the content it matched."""
    result = redactor.redact("Anna Smith called")
    assert result.rules_fired
    assert "Anna" not in " ".join(result.rules_fired)
    assert "Smith" not in " ".join(result.rules_fired)


def test_clean_message_is_untouched(redactor):
    text = "the filing was published on tuesday"
    result = redactor.redact(text)
    assert result.text == text and result.rules_fired == ()


def test_empty_message(redactor):
    assert redactor.redact("").text == ""
