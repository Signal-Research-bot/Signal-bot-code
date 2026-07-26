"""Backfill the cache from a Signal Android backup.

signal-cli never backfills: it receives from the moment it is linked and no
earlier. Anything said before that exists only on the phones. This module is the
one supported way to get it in.

    Signal (Android) -> Settings -> Chats -> Backups -> Turn on
    (save the 30-digit passphrase; without it the file is unreadable, forever)

    signalbackup-tools <file.backup> <passphrase> --exportcsv \\
        message=message.csv,recipient=recipient.csv,groups=groups.csv,thread=thread.csv

    python -m signal_research_bot.importer --csv-dir <dir> --dry-run

WHY CSV AND NOT THE RENDERED EXPORT
-----------------------------------
signalbackup-tools can also emit HTML or plain text, and `sigexport` produces
Markdown from Signal Desktop. Both were rejected for the same reason: they carry
DISPLAY NAMES, not ACIs.

`PseudonymStore` keys on the ACI. It is what makes `Participant C` mean the same
person in every entry for the life of the archive. Import keyed on display names
instead and the same person receives one label in imported entries and another
in live ones -- an archive that looks consistent and quietly is not. The CSV
dump preserves the recipient table, so the real ACIs come through and imported
messages land in the same identity space as live ones.

THE FILE YOU ARE HANDLING
-------------------------
A Signal backup contains EVERY conversation on the account, not just the group
being researched. It is the most sensitive artefact this project touches. This
module filters to one group and discards the rest before anything is written --
and the filter is applied before any other field is read, because the reverse
order is exactly the bug that shipped in the receiver on 2026-07-26, where
display names were harvested from every conversation on the account because the
group check ran one step too late.

Nothing here prints or logs a message body.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from .cache import Cache, CacheEncryptionUnavailable
from .config import Config, ConfigError
from .envelope import SELF, Kind, ParsedMessage, coarsen
from .logging_setup import configure

log = logging.getLogger(__name__)

# Signal Android encodes the message kind in the low bits of `type`.
# Outgoing base types, from MessageTypes in the Signal Android source.
_BASE_TYPE_MASK = 0x1F
_OUTGOING_BASE_TYPES = frozenset({21, 22, 23, 24, 25, 26, 27})

# Signal writes group ids in more than one surface form. Compare raw bytes.
_GROUP_PREFIX = re.compile(r"^__signal_group__v\d+__!?", re.I)


class ImportError_(RuntimeError):
    """The export could not be read, or does not contain what was asked for."""


def normalise_group_id(raw: str) -> bytes | None:
    """Reduce any written form of a group id to its raw bytes.

    The same group appears as base64 in `.env` (which is what signal-cli's
    JSON-RPC hands back) and, in an Android backup, as hex behind a
    `__signal_group__v2__!` prefix. Comparing the strings finds nothing;
    comparing the decoded bytes is exact.
    """
    if not raw:
        return None
    text = _GROUP_PREFIX.sub("", raw.strip())
    if not text:
        return None

    # Hex first: a hex string is also valid base64 input and would decode to
    # the wrong bytes silently.
    if re.fullmatch(r"[0-9a-fA-F]+", text) and len(text) % 2 == 0:
        try:
            return binascii.unhexlify(text)
        except binascii.Error:
            pass
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
    except (binascii.Error, ValueError):
        return None


def _column(header: Sequence[str], *candidates: str) -> str:
    """First column present, by name. Raises rather than guessing.

    Signal's schema has been renamed across versions -- `recipient_id` became
    `from_recipient_id`, `uuid` became `aci` -- and the export reflects whatever
    version made the backup. Failing loudly beats importing a column of empty
    strings and producing a silently empty window.
    """
    lower = {c.lower(): c for c in header}
    for name in candidates:
        if name.lower() in lower:
            return lower[name.lower()]
    raise ImportError_(
        f"none of {candidates!r} present in the export. Columns found: "
        f"{sorted(header)[:25]}"
    )


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ImportError_(f"{path.name} not found. Was it included in --exportcsv?")
    with path.open(encoding="utf-8", errors="replace", newline="") as fh:
        return list(csv.DictReader(fh))


def _is_outgoing(raw_type: str) -> bool:
    try:
        return (int(raw_type) & _BASE_TYPE_MASK) in _OUTGOING_BASE_TYPES
    except (TypeError, ValueError):
        return False


@dataclass
class ImportStats:
    rows_read: int = 0
    other_group: int = 0
    no_body: int = 0
    unknown_sender: int = 0
    remote_deleted: int = 0
    imported: int = 0
    duplicates: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def resolve_thread_ids(csv_dir: Path, target: bytes) -> set[str]:
    """Thread ids belonging to the target group, and nothing else.

    Applied before any message field is read. groups -> recipient -> thread.
    """
    groups = _rows(csv_dir / "groups.csv")
    if not groups:
        raise ImportError_("groups.csv is empty; nothing to match against")
    gid_col = _column(groups[0].keys(), "group_id", "_id")
    grec_col = _column(groups[0].keys(), "recipient_id", "recipient")

    recipient_ids = {
        row[grec_col]
        for row in groups
        if normalise_group_id(row.get(gid_col, "")) == target
    }
    if not recipient_ids:
        raise ImportError_(
            "the target group id was not found in groups.csv. Check SRB_GROUP_ID "
            "matches the group this backup came from."
        )

    threads = _rows(csv_dir / "thread.csv")
    tid_col = _column(threads[0].keys(), "_id", "id")
    trec_col = _column(threads[0].keys(), "recipient_id", "thread_recipient_id", "recipient")
    return {r[tid_col] for r in threads if r.get(trec_col) in recipient_ids}


def load_acis(csv_dir: Path) -> dict[str, str]:
    """recipient._id -> ACI. The identity space the live pipeline already uses."""
    rows = _rows(csv_dir / "recipient.csv")
    if not rows:
        raise ImportError_("recipient.csv is empty")
    id_col = _column(rows[0].keys(), "_id", "id")
    aci_col = _column(rows[0].keys(), "aci", "uuid", "service_id", "serviceId")
    out = {}
    for row in rows:
        aci = (row.get(aci_col) or "").strip()
        if aci:
            out[row[id_col]] = aci.lower()
    return out


def parse(csv_dir: Path, target: bytes, *, since_ms: int = 0) -> tuple[list[ParsedMessage], ImportStats]:
    """Read the export into ParsedMessages. Group filter first, always."""
    stats = ImportStats()
    threads = resolve_thread_ids(csv_dir, target)
    acis = load_acis(csv_dir)

    rows = _rows(csv_dir / "message.csv")
    if not rows:
        raise ImportError_("message.csv is empty")
    header = rows[0].keys()
    thread_col = _column(header, "thread_id", "thread")
    from_col = _column(header, "from_recipient_id", "recipient_id", "address")
    date_col = _column(header, "date_sent", "date", "date_received")
    body_col = _column(header, "body")
    type_col = _column(header, "type", "msg_box")
    deleted_col = next(
        (c for c in header if c.lower() in ("remote_deleted", "remote_deleted_")), None
    )

    out: list[ParsedMessage] = []
    for row in rows:
        stats.rows_read += 1

        # FIRST. Nothing else is read from a row belonging to another chat.
        if row.get(thread_col) not in threads:
            stats.other_group += 1
            continue

        if deleted_col and str(row.get(deleted_col, "")).strip() in ("1", "true", "True"):
            # Sender deleted it for everyone. It never enters the cache; the
            # live path drops these too.
            stats.remote_deleted += 1
            continue

        body = (row.get(body_col) or "").strip()
        if not body:
            # Call logs, group updates, profile changes and attachment-only
            # rows. Nothing researchable, and they would only cost tokens.
            stats.no_body += 1
            continue

        try:
            ts = int(row.get(date_col) or 0)
        except ValueError:
            ts = 0
        if ts <= 0 or ts < since_ms:
            continue

        if _is_outgoing(row.get(type_col, "")):
            source = SELF          # matches how the live receiver labels own messages
        else:
            source = acis.get(row.get(from_col, ""), "")
            if not source:
                # A sender with no ACI cannot be pseudonymised consistently, and
                # guessing would put them in a different identity space from
                # the live pipeline. Dropped and counted.
                stats.unknown_sender += 1
                continue

        out.append(
            ParsedMessage(
                kind=Kind.MESSAGE,
                group_id="",              # set by the caller; not used downstream
                source=source,
                timestamp_ms=coarsen(ts),
                body=body,
                raw_timestamp_ms=ts,
            )
        )
    return out, stats


def run(cfg: Config, csv_dir: Path, *, since_ms: int = 0, dry_run: bool = False) -> int:
    target = normalise_group_id(cfg.group_id or "")
    if not target:
        raise ImportError_("SRB_GROUP_ID is unset or not decodable")

    messages, stats = parse(csv_dir, target, since_ms=since_ms)
    messages.sort(key=lambda m: m.raw_timestamp_ms)

    if dry_run:
        # Counts only. A dry run of an import must never print message content:
        # the whole point is to check the filter before trusting it.
        print("-- import dry run: nothing written --")
        print(f"  would import : {len(messages)}")
        for k, v in stats.as_dict().items():
            print(f"  {k:16} {v}")
        if messages:
            span = (messages[-1].raw_timestamp_ms - messages[0].raw_timestamp_ms) / 86_400_000
            print(f"  span         : {span:.1f} days")
        return 0

    cache = Cache.open(cfg.cache_path, cfg.cache_key)
    try:
        for msg in messages:
            # The cache primary key is (source, raw_timestamp), so re-running an
            # import is safe and messages already collected live are not
            # duplicated.
            if cache.add(msg):
                stats.imported += 1
            else:
                stats.duplicates += 1
    finally:
        cache.close()

    log.info("import complete", extra=stats.as_dict())
    print(f"imported {stats.imported} message(s); {stats.duplicates} already present")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv-dir", required=True, type=Path,
                    help="directory holding message.csv, recipient.csv, groups.csv, thread.csv")
    ap.add_argument("--since", default="",
                    help="only import messages on or after this date (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be imported, write nothing")
    args = ap.parse_args()

    since_ms = 0
    if args.since:
        import datetime as _dt  # noqa: PLC0415
        try:
            d = _dt.datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            print("--since must be YYYY-MM-DD", file=sys.stderr)
            return 2
        since_ms = int(d.timestamp() * 1000)

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure(cfg.log_level)
    try:
        return run(cfg, args.csv_dir, since_ms=since_ms, dry_run=args.dry_run)
    except (ImportError_, CacheEncryptionUnavailable) as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
