"""Logging that structurally cannot emit message content or host paths.

Two rules, both enforced here rather than left to reviewer discipline:

* Log records carry structured fields, never interpolated message text. The
  formatter emits the message plus explicitly-listed extras, so a stray
  f-string with a message body in it is a visible mistake rather than a
  silently-shipped leak.
* Paths are rendered relative to the repo. An absolute path in a log line
  carries the operator's username, and logs get pasted into issues.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fields the standard library puts on every record; anything else is ours.
_STANDARD = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName
    relativeCreated stack_info taskName thread threadName""".split()
)

# Never emit these, whatever an extra is called.
_FORBIDDEN_KEYS = frozenset({"body", "message_text", "text", "content", "transcript"})


def _relativise(value: object) -> object:
    if isinstance(value, Path):
        try:
            return value.resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return value.name          # outside the repo: filename only
    return value


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            if key in _FORBIDDEN_KEYS:
                payload[key] = "<redacted>"
                continue
            payload[key] = _relativise(value)
        if record.exc_info:
            # Type only. A traceback can carry local variables holding message
            # content into the log.
            payload["exc"] = record.exc_info[0].__name__ if record.exc_info[0] else "?"
        return json.dumps(payload, ensure_ascii=False)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("SRB_LOG_LEVEL", level).upper())
