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


# A Signal group id is base64 of 32 bytes -- 44 characters. The egress firewall
# checks for it by substring, which is correct for a real id and catastrophic
# for a short one: "g" occurs in almost any text, so every batch would be
# blocked with no obvious cause. Catching it here turns a baffling runtime
# symptom into a config error naming the variable.
MIN_GROUP_ID_LEN = 16

# What this archive is ABOUT. Without it the pipeline researches whatever comes
# up -- a restaurant recommendation scores as well as a securities question --
# which is the fastest way to waste money and clutter the archive.
#
# Scoping here rather than asking members to phrase things a particular way is
# deliberate: the interesting leads arrive in ordinary conversation, and any
# rule that depends on eight people remembering a syntax will not hold.
DEFAULT_DOMAIN = (
    "Financial and corporate investigation of the Bitcoin and crypto sector. "
    "In scope: ownership structures, related-party transactions, undisclosed "
    "connections between companies and people; reserves, attestations, audits "
    "and their absence; regulatory action, enforcement, litigation and "
    "filings; entities tied to Cantor Fitzgerald, Tether, Bitfinex and their "
    "affiliates, investors and counterparties; the funding and governance of "
    "Bitcoin development; claims of corruption, conflicts of interest or "
    "market manipulation anywhere in that sector. "
    "Out of scope: price predictions, trading opinions, general crypto news "
    "with no investigative angle, technical support, and anything unrelated "
    "to the sector."
)


def _require_group_id() -> str:
    value = _require("SRB_GROUP_ID")
    if len(value) < MIN_GROUP_ID_LEN:
        raise ConfigError(
            f"SRB_GROUP_ID is {len(value)} characters, which is too short to be "
            f"a real Signal group id (they are 44). A short value would match "
            f"as a substring of ordinary text and block every batch. "
            f"Run `signal-cli --config /data listGroups` to get the real one."
        )
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
    observed_handles_path: Path
    auto_handles: bool
    pseudonyms_path: Path
    quarantine_dir: Path
    metrics_path: Path
    kb_dir: Path | None
    foreign_vault_dir: Path | None
    log_level: str
    max_tasks_per_window: int
    worth_threshold: float
    notify: bool
    research_domain: str

    @classmethod
    def from_env(cls) -> "Config":
        cache_dir = _path("SRB_CACHE_DIR", "var")
        var_dir = _path("SRB_VAR_DIR", "var")
        kb = os.environ.get("SRB_KB_DIR", "").strip()
        foreign = os.environ.get("SRB_FOREIGN_VAULT_DIR", "").strip()
        return cls(
            signal_host=os.environ.get("SRB_SIGNAL_HOST", "signal-cli"),
            signal_port=int(os.environ.get("SRB_SIGNAL_PORT", "7583")),
            group_id=_require_group_id(),
            cache_path=cache_dir / "messages.db",
            cache_key=os.environ.get("SRB_CACHE_KEY") or None,
            roster_path=var_dir / "roster.json",
            observed_handles_path=var_dir / "observed-handles.json",
            auto_handles=os.environ.get("SRB_AUTO_HANDLES", "1") not in ("0", "false", "no"),
            pseudonyms_path=var_dir / "pseudonyms.json",
            quarantine_dir=var_dir / "quarantine",
            metrics_path=var_dir / "metrics.jsonl",
            kb_dir=Path(kb) if kb else None,
            foreign_vault_dir=Path(foreign) if foreign else None,
            log_level=os.environ.get("SRB_LOG_LEVEL", "INFO").upper(),
            max_tasks_per_window=int(os.environ.get("SRB_MAX_TASKS_PER_WINDOW", "4")),
            worth_threshold=float(os.environ.get("SRB_WORTH_THRESHOLD", "0.6")),
            notify=os.environ.get("SRB_NOTIFY", "false").lower() in {"1", "true", "yes"},
            research_domain=os.environ.get("SRB_RESEARCH_DOMAIN", DEFAULT_DOMAIN).strip(),
        )
