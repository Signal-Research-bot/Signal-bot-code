"""Tests for the encrypted message cache.

The cache holds unredacted content, so the encryption test is not ceremony: a
misconfigured key pragma produces a perfectly working database that is not
encrypted at all, and nothing else in the system would notice.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_research_bot.cache import (  # noqa: E402
    Cache,
    CacheEncryptionUnavailable,
    looks_encrypted,
)
from signal_research_bot.envelope import Kind, Mention, ParsedMessage  # noqa: E402

ALICE = str(uuid.UUID(int=0xA11CE))
KEY = "00" * 32
TS = 1_784_000_000_000


def msg(ts: int = TS, source: str = ALICE, **kw) -> ParsedMessage:
    defaults = dict(
        kind=Kind.MESSAGE,
        group_id="g",
        source=source,
        timestamp_ms=ts - (ts % (15 * 60 * 1000)),
        body="a message",
        raw_timestamp_ms=ts,
    )
    defaults.update(kw)
    return ParsedMessage(**defaults)


@pytest.fixture
def cache(tmp_path):
    # allow_plaintext only ever appears in tests, and only with synthetic data.
    c = Cache.open(tmp_path / "c.db", allow_plaintext=True)
    yield c
    c.close()


# --- encryption is real, not assumed -----------------------------------------


def test_encrypted_db_is_not_a_readable_sqlite_file(tmp_path):
    path = tmp_path / "enc.db"
    c = Cache.open(path, KEY)
    c.add(msg())
    c.close()
    assert looks_encrypted(path), "cache file is plaintext despite a key"


def test_plaintext_db_is_detected_as_plaintext(tmp_path):
    """Proves looks_encrypted can actually tell the difference."""
    path = tmp_path / "plain.db"
    c = Cache.open(path, allow_plaintext=True)
    c.add(msg())
    c.close()
    assert not looks_encrypted(path)


def test_encrypted_db_cannot_be_opened_without_the_key(tmp_path):
    import sqlcipher3

    path = tmp_path / "enc.db"
    c = Cache.open(path, KEY)
    c.add(msg())
    c.close()

    con = sqlcipher3.connect(str(path))
    with pytest.raises(Exception):
        con.execute("SELECT COUNT(*) FROM messages").fetchone()


def test_refuses_plaintext_without_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)
    with pytest.raises((CacheEncryptionUnavailable, Exception)):
        Cache.open(tmp_path / "x.db", key=None)


def test_missing_key_is_refused(tmp_path):
    with pytest.raises(CacheEncryptionUnavailable):
        Cache.open(tmp_path / "x.db", key=None, allow_plaintext=False)


# --- dedupe is the database's job --------------------------------------------


def test_new_message_is_stored(cache):
    assert cache.add(msg()) is True
    assert cache.counts()["total"] == 1


def test_duplicate_is_rejected(cache):
    cache.add(msg())
    assert cache.add(msg()) is False
    assert cache.counts()["total"] == 1


def test_dedupe_survives_a_restart(tmp_path):
    """An in-memory seen-set would forget this across a crash."""
    path = tmp_path / "c.db"
    c1 = Cache.open(path, allow_plaintext=True)
    c1.add(msg())
    c1.close()

    c2 = Cache.open(path, allow_plaintext=True)
    assert c2.add(msg()) is False
    c2.close()


def test_same_timestamp_different_sender_is_distinct(cache):
    other = str(uuid.UUID(int=0xB0B))
    assert cache.add(msg()) and cache.add(msg(source=other))
    assert cache.counts()["total"] == 2


# --- retraction --------------------------------------------------------------


def test_remote_delete_tombstones_and_clears_the_body(cache):
    cache.add(msg())
    cache.apply(
        msg(ts=TS + 1000, kind=Kind.DELETE, target_timestamp_ms=TS, body="")
    )
    assert cache.counts()["retracted"] == 1
    assert cache.pending() == []


def test_retracted_message_never_reaches_a_batch(cache):
    """Even when it was cached before the retraction arrived."""
    cache.add(msg())
    assert len(cache.pending()) == 1
    cache.retract(ALICE, TS)
    assert cache.pending() == []


def test_edit_supersedes_the_original(cache):
    cache.add(msg(body="first draft"))
    cache.apply(
        msg(ts=TS + 5000, kind=Kind.EDIT, target_timestamp_ms=TS, body="corrected")
    )
    bodies = [m.body for m in cache.pending()]
    assert bodies == ["corrected"], "both versions must not survive as two claims"


def test_expiration_update_is_recorded_not_stored(cache):
    cache.apply(msg(kind=Kind.EXPIRATION_UPDATE))
    assert cache.get_state("expiration_changed_at") is not None
    assert cache.pending() == []


# --- batching ----------------------------------------------------------------


def test_pending_is_oldest_first(cache):
    for i in (3, 1, 2):
        cache.add(msg(ts=TS + i * 60_000))
    assert [m.raw_timestamp_ms for m in cache.pending()] == [
        TS + 60_000, TS + 120_000, TS + 180_000
    ]


def test_mark_processed_removes_from_pending(cache):
    cache.add(msg())
    batch = cache.pending()
    cache.mark_processed("w1", batch)
    assert cache.pending() == []


def test_pending_round_trips_mentions(cache):
    cache.add(msg(mentions=(Mention(3, 6, ALICE),)))
    got = cache.pending()[0].mentions
    assert got == (Mention(3, 6, ALICE),)


def test_counts_reports_no_content(cache):
    cache.add(msg(body="something private"))
    assert "something private" not in repr(cache.counts())


# --- state -------------------------------------------------------------------


def test_state_round_trip(cache):
    cache.set_state("last_seen_ms", "123")
    assert cache.get_state("last_seen_ms") == "123"


def test_state_upsert(cache):
    cache.set_state("k", "1")
    cache.set_state("k", "2")
    assert cache.get_state("k") == "2"
