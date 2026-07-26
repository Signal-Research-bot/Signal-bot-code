"""Tests for pseudonym derivation and the roster.

The properties here are the ones the whole privacy design rests on: labels are
stable, derivation is keyed and irreversible, and the key never lands somewhere
an attacker with the cache would also find it.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.identity import (  # noqa: E402
    KeyUnavailable,
    PseudonymStore,
    Roster,
    allowed_labels,
    internal_id,
    is_opted_out,
    load_or_create_key,
)

ALICE = str(uuid.UUID(int=0xA11CE))
BOB = str(uuid.UUID(int=0xB0B))
KEY = b"k" * 32
OTHER_KEY = b"j" * 32


# --- derivation ---------------------------------------------------------------


def test_same_aci_and_key_gives_the_same_id():
    assert internal_id(KEY, ALICE) == internal_id(KEY, ALICE)


def test_different_acis_give_different_ids():
    assert internal_id(KEY, ALICE) != internal_id(KEY, BOB)


def test_a_different_key_gives_a_different_id():
    """The key, not the algorithm, is what protects the mapping."""
    assert internal_id(KEY, ALICE) != internal_id(OTHER_KEY, ALICE)


def test_derivation_is_case_and_whitespace_insensitive():
    """The same account written two ways must not become two participants."""
    assert internal_id(KEY, ALICE.upper()) == internal_id(KEY, f"  {ALICE}  ")


def test_id_does_not_contain_the_aci():
    assert ALICE not in internal_id(KEY, ALICE)


def test_empty_aci_is_refused():
    """Deriving from nothing would map every unknown sender to one identity."""
    with pytest.raises(ValueError):
        internal_id(KEY, "")


# --- key handling -------------------------------------------------------------


def test_key_comes_from_the_environment_when_set(monkeypatch):
    monkeypatch.setenv("SRB_PSEUDONYM_KEY", "ab" * 32)
    assert load_or_create_key() == bytes.fromhex("ab" * 32)


def test_short_key_is_refused(monkeypatch):
    monkeypatch.setenv("SRB_PSEUDONYM_KEY", "abcd")
    with pytest.raises(KeyUnavailable):
        load_or_create_key()


def test_non_hex_key_is_refused(monkeypatch):
    monkeypatch.setenv("SRB_PSEUDONYM_KEY", "nothexatall" * 8)
    with pytest.raises(KeyUnavailable):
        load_or_create_key()


def test_refuses_to_invent_a_key_when_creation_is_disabled(monkeypatch):
    """Silently generating a fresh key would produce new pseudonyms every run,
    breaking cross-window dedupe and destroying the erasure-request path."""
    monkeypatch.delenv("SRB_PSEUDONYM_KEY", raising=False)
    monkeypatch.setattr(
        "signal_research_bot.identity._keyring",
        lambda: type("K", (), {"get_password": staticmethod(lambda *a: None)})(),
    )
    with pytest.raises(KeyUnavailable):
        load_or_create_key(allow_create=False)


# --- label allocation ---------------------------------------------------------


def test_labels_are_allocated_in_first_seen_order(tmp_path):
    store = PseudonymStore(KEY, tmp_path / "p.json")
    assert store.label(BOB) == "Participant A"
    assert store.label(ALICE) == "Participant B"


def test_label_is_stable_for_the_same_sender(tmp_path):
    store = PseudonymStore(KEY, tmp_path / "p.json")
    assert store.label(ALICE) == store.label(ALICE)


def test_labels_survive_a_restart(tmp_path):
    """'Participant A' must mean the same person in every window and entry."""
    path = tmp_path / "p.json"
    first = PseudonymStore(KEY, path)
    label = first.label(ALICE)
    assert PseudonymStore(KEY, path).label(ALICE) == label


def test_mapping_file_stores_derived_ids_not_acis(tmp_path):
    """The file sits next to the cache; it must not be a plaintext directory."""
    path = tmp_path / "p.json"
    PseudonymStore(KEY, path).label(ALICE)
    raw = path.read_text(encoding="utf-8")
    assert ALICE not in raw
    assert list(json.loads(raw).values()) == ["Participant A"]


def test_labels_roll_over_past_z(tmp_path):
    store = PseudonymStore(KEY, tmp_path / "p.json")
    labels = [store.label(str(uuid.UUID(int=i))) for i in range(27)]
    assert labels[25] == "Participant Z" and labels[26] == "Participant AA"


def test_known_labels_reports_everything_allocated(tmp_path):
    store = PseudonymStore(KEY, tmp_path / "p.json")
    store.label(ALICE)
    store.label(BOB)
    assert store.known_labels() == {"Participant A", "Participant B"}


def test_allowed_labels_matches_allocation_order():
    assert allowed_labels(3) == {"Participant A", "Participant B", "Participant C"}


# --- roster -------------------------------------------------------------------


def test_name_variants_cover_parts_and_possessives():
    variants = Roster(names=("Anna Smith",)).name_variants()
    assert {"Anna Smith", "Anna", "Smith", "Anna's", "Smith's"} <= variants


def test_single_letter_parts_are_excluded():
    """Initials would match everywhere and make the redactor unusable."""
    assert "A" not in Roster(names=("A Smith",)).name_variants()


def test_opt_out_is_by_aci():
    roster = Roster(names=("x",), opted_out=frozenset({BOB}))
    assert is_opted_out(roster, BOB)
    assert not is_opted_out(roster, ALICE)


def test_opt_out_of_an_unknown_sender_is_false():
    assert not is_opted_out(Roster(), None)


def test_roster_round_trips_through_a_file(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(
        json.dumps({"names": ["Anna Smith"], "phones": ["+" + "15551234567"],
                    "group_name": "G", "opted_out": [BOB]}),
        encoding="utf-8",
    )
    roster = Roster.load(path)
    assert roster.names == ("Anna Smith",) and roster.opted_out == frozenset({BOB})


def test_roster_template_is_rejected_until_edited(tmp_path):
    """The shipped template must not load as-is.

    Reads the real template rather than a fixture, so this fails if the two
    ever drift -- a template whose placeholder no longer matches the guard is
    worse than no guard, because it reintroduces the silent-success case it
    exists to prevent.
    """
    template = Path(__file__).resolve().parent.parent / "roster.example.json"
    assert template.exists(), "the roster template is missing"

    target = tmp_path / "roster.json"
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(KeyUnavailable) as caught:
        Roster.load(target)
    assert "placeholder" in str(caught.value)


def test_an_edited_roster_loads(tmp_path):
    """The counterweight: the guard must not block a real roster."""
    target = tmp_path / "roster.json"
    target.write_text(
        json.dumps({
            "names": ["Anna Smith", "Annie"],
            "phones": ["+" + "44" + "2079250918"],
            "opted_out": [],
            "group_name": "Ravenhill",
        }),
        encoding="utf-8",
    )
    roster = Roster.load(target)
    assert "Annie" in roster.names
    assert roster.group_name == "Ravenhill"
