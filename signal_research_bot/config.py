"""Configuration, entirely from the environment.

There are no absolute paths in this repository. Every location is an env var
with a relative default, which is what keeps the operator's directory layout
(and therefore their username) out of tracked files by construction rather
than by remembering. tools/scrub_check.py enforces it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """A required setting is missing or unusable."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. See .env.example.")
    return value


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass(frozen=True)
class Config:
    signal_host: str
    signal_port: int
    group_id: str
    cache_path: Path
    cache_key: str | None
    roster_path: Path
    pseudonyms_path: Path
    quarantine_dir: Path
    metrics_path: Path
    kb_dir: Path | None
    foreign_vault_dir: Path | None
    log_level: str
    max_tasks_per_window: int
    worth_threshold: float

    @classmethod
    def from_env(cls) -> "Config":
        cache_dir = _path("SRB_CACHE_DIR", "var")
        var_dir = _path("SRB_VAR_DIR", "var")
        kb = os.environ.get("SRB_KB_DIR", "").strip()
        foreign = os.environ.get("SRB_FOREIGN_VAULT_DIR", "").strip()
        return cls(
            signal_host=os.environ.get("SRB_SIGNAL_HOST", "signal-cli"),
            signal_port=int(os.environ.get("SRB_SIGNAL_PORT", "7583")),
            group_id=_require("SRB_GROUP_ID"),
            cache_path=cache_dir / "messages.db",
            cache_key=os.environ.get("SRB_CACHE_KEY") or None,
            roster_path=var_dir / "roster.json",
            pseudonyms_path=var_dir / "pseudonyms.json",
            quarantine_dir=var_dir / "quarantine",
            metrics_path=var_dir / "metrics.jsonl",
            kb_dir=Path(kb) if kb else None,
            foreign_vault_dir=Path(foreign) if foreign else None,
            log_level=os.environ.get("SRB_LOG_LEVEL", "INFO").upper(),
            max_tasks_per_window=int(os.environ.get("SRB_MAX_TASKS_PER_WINDOW", "4")),
            worth_threshold=float(os.environ.get("SRB_WORTH_THRESHOLD", "0.6")),
        )
