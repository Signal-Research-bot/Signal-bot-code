"""Tests for the egress firewall.

This is the suite that carries the project's privacy claim. Every fixture is
SYNTHETIC, and per the repo invariant no phone-, email- or UUID-shaped literal
is written directly -- they are assembled at runtime.

The structure mirrors the firewall's three assertions:
  1. known identity (roster names, roster phones, group name, group id)
  2. identity *shapes* (uuid, email, phone) -- catches what the roster missed
  3. speaker labels must be ones we allocated
plus request shape, quarantine behaviour, and evasion attempts.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.egress import (  # noqa: E402
    EgressViolation,
    Policy,
    check_inbound,
    check_outbound,
    guard,
    quarantine,
)
from signal_research_bot.identity import Roster  # noqa: E402

ROSTER_PHONE = "+" + "1" + "415" + "555" + "0123"
STRANGER_PHONE = "+" + "44" + "7700" + "900" + "123"
FAKE_UUID = str(uuid.UUID(int=0xFEED))
FAKE_EMAIL = "someone" + "@" + "example" + ".invalid"
GROUP_ID = "Zm9vYmFyZ3JvdXBpZA=="


@pytest.fixture
def policy():
    return Policy.build(
        roster=Roster(
            names=("Anna Smith", "Bo"),
            phones=(ROSTER_PHONE,),
            group_name="Ravenhill",
        ),
        allowed_labels={"Participant A", "Participant B"},
        group_id=GROUP_ID,
    )


def body(text: str) -> dict:
    """A minimally realistic Messages API request."""
    return {
        "model": "claude-sonnet-5",
        "max_tokens": 4096,
        "system": "Extract research tasks from the transcript.",
        "messages": [{"role": "user", "content": text}],
    }


def blocked(payload, policy) -> str:
    with pytest.raises(EgressViolation) as exc:
        check_outbound(payload, policy)
    return exc.value.rule


# --- a clean payload passes ---------------------------------------------------


def test_clean_transcript_passes(policy):
    sha = check_outbound(
        body("Participant A: is the reserve figure audited?\n"
             "Participant B: the filing says otherwise."),
        policy,
    )
    assert len(sha) == 12


def test_research_content_is_not_collateral_damage(policy):
    """The firewall must not block the content it exists to protect."""
    check_outbound(
        body(
            "Participant A: reserves hit 3200000000 in the 2024 10-K, "
            "see https://www.sec.gov/Archives/edgar/data/1/f.htm and 0x"
            + "a" * 40
        ),
        policy,
    )


# --- assertion 1: known identity ----------------------------------------------


def test_roster_name_blocks(policy):
    assert blocked(body("Anna Smith asked about reserves"), policy) == "roster-name"


def test_roster_first_name_blocks(policy):
    assert blocked(body("ask Anna"), policy) == "roster-name"


def test_roster_possessive_blocks(policy):
    assert blocked(body("that was Anna's point"), policy) == "roster-name"


def test_roster_name_blocks_wherever_it_hides(policy):
    """Serialization is why this works: the name is in a tool description,
    not in the message content a dict walk would think to inspect."""
    payload = body("Participant A: fine")
    payload["tools"] = [{"name": "lookup", "description": "ask Anna Smith first"}]
    assert blocked(payload, policy) == "roster-name"


def test_roster_name_in_system_prompt_blocks(policy):
    payload = body("Participant A: fine")
    payload["system"] = "You are helping Anna Smith with research."
    assert blocked(payload, policy) == "roster-name"


def test_group_name_blocks(policy):
    assert blocked(body("the Ravenhill group thinks"), policy) == "group-name"


def test_group_id_blocks(policy):
    assert blocked(body(f"from group {GROUP_ID}"), policy) == "group-id"


def test_roster_phone_blocks_despite_reformatting(policy):
    """Digits are compared, so punctuation cannot smuggle a known number out."""
    reformatted = "(415) 555-0123"
    assert blocked(body(f"call {reformatted}"), policy) in {
        "roster-phone",
        "separated-phone",
    }


# --- assertion 2: identity shapes, for what the roster missed ------------------


def test_uuid_blocks(policy):
    assert blocked(body(f"aci {FAKE_UUID}"), policy) == "uuid"


def test_email_blocks(policy):
    assert blocked(body(f"mail {FAKE_EMAIL}"), policy) == "email"


def test_phone_of_someone_not_in_the_roster_blocks(policy):
    """The roster is a closed world; this is the layer that covers its edges."""
    assert blocked(body(f"try {STRANGER_PHONE}"), policy) == "e164-phone"


# --- assertion 3: speaker labels ----------------------------------------------


def test_unallocated_label_blocks(policy):
    """A label we never issued means the pseudonym table and the batch
    disagree -- which usually means the batch was built from stale state."""
    assert blocked(body("Participant Q: hello"), policy) == "unknown-label"


def test_allocated_labels_pass(policy):
    check_outbound(body("Participant A and Participant B agree"), policy)


# --- request shape ------------------------------------------------------------


@pytest.mark.parametrize("key", ["mcp_servers", "file_id", "container", "requests"])
def test_forbidden_request_keys_block(policy, key):
    payload = body("Participant A: fine")
    payload[key] = "anything"
    assert blocked(payload, policy) == "request-shape"


def test_forbidden_key_nested_deeply_blocks(policy):
    payload = body("Participant A: fine")
    payload["messages"][0]["content"] = [{"type": "text", "file_id": "f_1"}]
    assert blocked(payload, policy) == "request-shape"


# --- evasion ------------------------------------------------------------------


# Each of these carries exactly ONE identity token, and it is the obfuscated
# one. An earlier version wrote "An<ZWSP>na Smith called", which passed on the
# intact "Smith" and so never exercised the evasion path at all -- mutation
# testing caught it. Keep these strings free of any other roster token.


def test_zero_width_space_inside_a_name_still_blocks(policy):
    """A ZWSP renders as nothing and defeats a naive substring check."""
    assert blocked(body("An​na called"), policy) == "roster-name"


def test_zero_width_joiner_inside_a_name_still_blocks(policy):
    assert blocked(body("An‍na called"), policy) == "roster-name"


def test_fullwidth_characters_still_block(policy):
    assert blocked(body("Ａｎｎａ said"), policy) == "roster-name"


def test_soft_hyphen_inside_a_name_still_blocks(policy):
    assert blocked(body("An­na said"), policy) == "roster-name"


def test_word_joiner_inside_a_name_still_blocks(policy):
    assert blocked(body("An⁠na said"), policy) == "roster-name"


# --- inbound: the model can echo back what it was given -----------------------


def test_inbound_clean_response_passes(policy):
    check_inbound("Participant A asked about audited reserves.", policy)


def test_inbound_response_containing_a_name_blocks(policy):
    with pytest.raises(EgressViolation):
        check_inbound("Anna Smith asked about reserves.", policy)


def test_inbound_response_containing_a_uuid_blocks(policy):
    with pytest.raises(EgressViolation):
        check_inbound(f"source id {FAKE_UUID}", policy)


# --- violations carry no content ----------------------------------------------


def test_violation_never_contains_the_offending_text(policy):
    with pytest.raises(EgressViolation) as exc:
        check_outbound(body("Anna Smith called about the audit"), policy)
    blob = f"{exc.value} {exc.value.detail} {exc.value.rule}"
    assert "Anna" not in blob and "audit" not in blob
    assert exc.value.payload_sha and len(exc.value.payload_sha) == 12


def test_same_payload_gives_a_stable_sha_for_correlation(policy):
    a = body("Participant A: one")
    assert check_outbound(a, policy) == check_outbound(dict(a), policy)


# --- quarantine ---------------------------------------------------------------


def test_guard_quarantines_on_violation(policy, tmp_path):
    payload = body("Anna Smith called")
    with pytest.raises(EgressViolation):
        guard(payload, policy, tmp_path)

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["rule"] == "roster-name"
    # The quarantine file DOES hold the payload -- that is its purpose. It is
    # gitignored and covered by the cache retention policy.
    assert "Anna Smith" in json.dumps(saved["payload"])


def test_guard_writes_nothing_when_clean(policy, tmp_path):
    guard(body("Participant A: fine"), policy, tmp_path)
    assert not list(tmp_path.glob("*.json"))


def test_quarantine_filename_carries_rule_and_sha(policy, tmp_path):
    try:
        check_outbound(body("Anna Smith"), policy)
    except EgressViolation as v:
        path = quarantine(body("Anna Smith"), v, tmp_path)
        assert v.payload_sha in path.name and v.rule in path.name


# --- false positives that would block every batch -----------------------------


def test_iso_date_is_not_a_phone_number(policy):
    """Regression found by an end-to-end dry run.

    The transcript builder writes "--- 2026-07-14 03:30 UTC ---" headers, and
    the separated-phone shape matches an ISO date. The firewall was blocking
    its own output on every single batch.
    """
    check_outbound(body("--- 2026-07-14 03:30 UTC ---\nParticipant A: hello"), policy)


@pytest.mark.parametrize(
    "text",
    [
        "filed 2024-01-31 and restated 2025-12-01",
        "between 2019-03 and 2021-11 the figure moved",
        "reserves hit 3200000000 in Q1",
        "see section 10-K-2 paragraph 4",
    ],
)
def test_ordinary_dates_and_figures_pass(policy, text):
    check_outbound(body(f"Participant A: {text}"), policy)


def test_a_separated_phone_is_still_caught(policy):
    """The loosened rule must not stop catching what it exists for.

    Uses a number NOT in the roster, so this exercises the shape rule rather
    than the more specific roster-digit check that would otherwise fire first.
    """
    formatted = "212" + "-" + "555" + "-" + "0198"
    assert blocked(body(f"Participant A: call {formatted}"), policy) == "separated-phone"


def test_a_separated_phone_with_spaces_is_caught(policy):
    spaced = "212" + " " + "555" + " " + "0198"
    assert blocked(body(f"Participant A: call {spaced}"), policy) == "separated-phone"


# --- audit regressions --------------------------------------------------------


def test_a_phone_number_without_a_plus_is_blocked(policy):
    """E164_RE requires the '+' and SEPARATED_PHONE_RE requires a separator, so
    a bare run of digits matched neither. The firewall is the backstop for a
    redaction miss, so this had to hold here too, not only in redact.py."""
    with pytest.raises(EgressViolation):
        check_outbound(
            {"messages": [{"role": "user", "content": "ring " + "44" + "2079250918"}]},
            policy,
        )


def test_a_reserves_figure_is_not_blocked(policy):
    """The firewall failing closed on every balance-sheet number would wedge
    every window that discussed one."""
    for figure in ("127000000000", "3200000000", "2024-03-31", "830000"):
        check_outbound(
            {"messages": [{"role": "user", "content": f"reserves {figure}"}]}, policy
        )


def test_non_ascii_digits_do_not_slip_past_the_firewall(policy):
    arabic = "".join(chr(0x0660 + int(d)) for d in "442079250918")
    with pytest.raises(EgressViolation):
        check_outbound({"messages": [{"role": "user", "content": arabic}]}, policy)


def test_a_bidi_control_does_not_hide_a_roster_name(policy):
    with pytest.raises(EgressViolation):
        check_outbound(
            {"messages": [{"role": "user", "content": "An" + "‮" + "na Smith said so"}]},
            policy,
        )


def test_a_roster_phone_in_arabic_digits_is_still_recognised():
    """The roster-phone check strips to ASCII digits and does a substring
    match, so an unfolded Arabic-Indic number reduces to the empty string and
    the check goes blind. Folding is what keeps it seeing.

    The bare-phone rule alone does not cover this: libphonenumber normalises
    Unicode digits internally, so that rule catches the number either way. This
    test pins the layer that does not.
    """
    number = "+" + "44" + "2079250918"
    roster = Roster(names=("Anna Smith",), phones=(number,), group_name="Ravenhill")
    pol = Policy.build(roster, {"Participant A"}, GROUP_ID)
    arabic = "".join(chr(0x0660 + int(d)) for d in number.lstrip("+"))
    with pytest.raises(EgressViolation) as caught:
        check_outbound({"messages": [{"role": "user", "content": arabic}]}, pol)
    assert caught.value.rule == "roster-phone"
