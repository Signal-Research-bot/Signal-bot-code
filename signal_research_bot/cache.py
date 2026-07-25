"""Durable, encrypted message cache.

This file holds **unredacted** message content: it is the raw store that the
batch stage later pseudonymises and redacts. That is why it is SQLCipher and
not sqlite3, and why `open_cache` refuses to fall back to plaintext.

It also has to survive the receiver crashing between "signal-cli handed us a
message" and "we finished with it". Writes are therefore the first thing that
happens to an inbound message, before any parsing that could raise.

Location: a Docker **named volume**, never a bind mount from the Windows host.
SQLite locking over 9p/drvfs is a known-fragile path, and a named volume also
means `git add -f .` structurally cannot reach the file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Iterator

from .envelope import Kind, Mention, ParsedMessage

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    source            TEXT    NOT NULL,
    raw_timestamp     INTEGER NOT NULL,
    coarse_timestamp  INTEGER NOT NULL,
    kind              TEXT    NOT NULL,
    body              TEXT    NOT NULL DEFAULT '',
    mentions          TEXT    NOT NULL DEFAULT '[]',
    quote_author      TEXT,
    quote_text        TEXT,
    attachment_count  INTEGER NOT NULL DEFAULT 0,
    -- Retraction, not deletion: a remoteDelete must be able to reach a message
    -- that has already been sent to a batch, so rows are tombstoned in place.
    retracted         INTEGER NOT NULL DEFAULT 0,
    superseded_by     INTEGER,
    processed_window  TEXT,
    PRIMARY KEY (source, raw_timestamp)
);
CREATE INDEX IF NOT EXISTS idx_unprocessed
    ON messages (processed_window, coarse_timestamp);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class CacheEncryptionUnavailable(RuntimeError):
    """SQLCipher is not usable and plaintext was not explicitly permitted."""


def _connect(path: Path, key: str | None, *, allow_plaintext: bool):
    if not allow_plaintext:
        try:
            import sqlcipher3  # noqa: PLC0415
        except ImportError as exc:
            raise CacheEncryptionUnavailable(
                "sqlcipher3 is not installed. The cache holds unredacted "
                "message content; refusing to write it as plaintext. Install "
                "sqlcipher3-wheels, or pass allow_plaintext=True (tests only)."
            ) from exc
        if not key:
            raise CacheEncryptionUnavailable("no cache key supplied")
        con = sqlcipher3.connect(str(path), isolation_level=None)
        # Must be the first statement on the connection.
        con.execute(f"PRAGMA key = \"x'{key}'\"")
    else:
        import sqlite3  # noqa: PLC0415

        con = sqlite3.connect(str(path), isolation_level=None)

    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=10000")   # concurrent receiver + batch job
    con.execute("PRAGMA foreign_keys=ON")
    return con


@dataclass
class Cache:
    """Thin wrapper over the connection. Deliberately not an ORM."""

    con: Any

    @classmethod
    def open(
        cls,
        path: Path,
        key: str | None = None,
        *,
        allow_plaintext: bool = False,
    ) -> "Cache":
        path.parent.mkdir(parents=True, exist_ok=True)
        con = _connect(path, key, allow_plaintext=allow_plaintext)
        con.executescript(SCHEMA)
        cache = cls(con)
        if cache.get_state("schema_version") is None:
            cache.set_state("schema_version", str(SCHEMA_VERSION))
        return cache

    # -- state ---------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self.con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- writes --------------------------------------------------------------

    def add(self, msg: ParsedMessage) -> bool:
        """Store a message. Returns False if it was already present.

        The primary key is (source, raw_timestamp), so dedupe is the database's
        job rather than a set held in memory that a restart would lose.
        """
        cur = self.con.execute(
            "INSERT OR IGNORE INTO messages "
            "(source, raw_timestamp, coarse_timestamp, kind, body, mentions, "
            " quote_author, quote_text, attachment_count) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                msg.source,
                msg.raw_timestamp_ms,
                msg.timestamp_ms,
                msg.kind.value,
                msg.body,
                json.dumps([[m.start, m.length, m.uuid] for m in msg.mentions]),
                msg.quote_author,
                msg.quote_text,
                msg.attachment_count,
            ),
        )
        return cur.rowcount > 0

    def retract(self, source: str, target_timestamp_ms: int) -> int:
        """Tombstone a message the sender deleted for everyone."""
        cur = self.con.execute(
            "UPDATE messages SET retracted=1, body='' "
            "WHERE source=? AND raw_timestamp=?",
            (source, target_timestamp_ms),
        )
        return cur.rowcount

    def supersede(self, source: str, target_timestamp_ms: int, by_timestamp_ms: int) -> int:
        """Mark an edited message as replaced, so both are never treated as
        two independent claims."""
        cur = self.con.execute(
            "UPDATE messages SET superseded_by=? WHERE source=? AND raw_timestamp=?",
            (by_timestamp_ms, source, target_timestamp_ms),
        )
        return cur.rowcount

    def apply(self, msg: ParsedMessage) -> bool:
        """Route a parsed message to the right write. Single entry point."""
        if msg.kind is Kind.DELETE and msg.target_timestamp_ms:
            self.retract(msg.source, msg.target_timestamp_ms)
            return True
        if msg.kind is Kind.EDIT and msg.target_timestamp_ms:
            self.supersede(msg.source, msg.target_timestamp_ms, msg.raw_timestamp_ms)
            # Stored as a MESSAGE, not an EDIT. EDIT is a transport signal, not
            # a storage category -- the payload *is* the new message body. Left
            # as kind=EDIT it would be filtered out of pending(), so the
            # original would be superseded and the correction never sent:
            # the edit would silently delete the message.
            return self.add(replace(msg, kind=Kind.MESSAGE))
        if msg.kind is Kind.EXPIRATION_UPDATE:
            self.set_state("expiration_changed_at", str(msg.raw_timestamp_ms))
            return True
        return self.add(msg)

    # -- reads ---------------------------------------------------------------

    def pending(self, limit: int = 5000) -> list[ParsedMessage]:
        """Messages not yet sent to a batch, oldest first.

        Excludes retracted and superseded rows: a message the sender withdrew
        must not reach the knowledge base, even if it was cached before the
        retraction arrived.
        """
        rows = self.con.execute(
            "SELECT source, raw_timestamp, coarse_timestamp, kind, body, mentions, "
            "       quote_author, quote_text, attachment_count "
            "FROM messages "
            "WHERE processed_window IS NULL AND retracted=0 AND superseded_by IS NULL "
            "  AND kind=? "
            "ORDER BY raw_timestamp ASC LIMIT ?",
            (Kind.MESSAGE.value, limit),
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    def mark_processed(self, window_id: str, msgs: Iterable[ParsedMessage]) -> None:
        self.con.executemany(
            "UPDATE messages SET processed_window=? WHERE source=? AND raw_timestamp=?",
            [(window_id, m.source, m.raw_timestamp_ms) for m in msgs],
        )

    def counts(self) -> dict[str, int]:
        """Operational telemetry. Counts only -- never content."""
        q = self.con.execute(
            "SELECT COUNT(*), "
            "  SUM(processed_window IS NULL), SUM(retracted), "
            "  SUM(superseded_by IS NOT NULL) FROM messages"
        ).fetchone()
        return {
            "total": q[0] or 0,
            "pending": q[1] or 0,
            "retracted": q[2] or 0,
            "superseded": q[3] or 0,
        }

    def close(self) -> None:
        self.con.close()


def _row_to_message(row: tuple) -> ParsedMessage:
    mentions = tuple(
        Mention(start=s, length=l, uuid=u) for s, l, u in json.loads(row[5])
    )
    return ParsedMessage(
        kind=Kind(row[3]),
        group_id="",                 # not stored: the cache is single-group
        source=row[0],
        timestamp_ms=row[2],
        body=row[4],
        mentions=mentions,
        quote_author=row[6],
        quote_text=row[7],
        attachment_count=row[8],
        raw_timestamp_ms=row[1],
    )


def looks_encrypted(path: Path) -> bool:
    """True if the file on disk is not a readable SQLite database.

    A plain sqlite file begins with the ASCII header 'SQLite format 3'. Its
    absence is what 'encrypted at rest' actually means here, and it is worth
    asserting rather than assuming -- a misconfigured key pragma produces a
    perfectly working database that is not encrypted at all.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as fh:
        return not fh.read(16).startswith(b"SQLite format 3")


def iter_windows(msgs: list[ParsedMessage], size: int) -> Iterator[list[ParsedMessage]]:
    for i in range(0, len(msgs), size):
        yield msgs[i : i + size]
