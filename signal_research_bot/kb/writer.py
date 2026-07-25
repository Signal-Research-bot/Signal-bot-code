"""Write records into the knowledge-base vault, and commit them.

Three safety properties, each of which exists because the vault is a directory
of hand-curated notes that a human also edits:

* **Creates only, never edits.** The bot writes new files under its own
  subdirectory. It never touches a file it did not create, so a bad run cannot
  damage anything a person wrote.
* **Refuses to write outside the vault.** Paths are resolved and checked, so a
  title containing traversal characters cannot escape.
* **Atomic.** Temp file plus os.replace, because the vault may be open in
  Obsidian and a half-written file is visible immediately.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

RESEARCH_SUBDIR = "Research Log"


class VaultError(RuntimeError):
    """A write was refused."""


@dataclass
class VaultWriter:
    vault_dir: Path
    foreign_vault_dir: Path | None = None
    subdir: str = RESEARCH_SUBDIR

    def __post_init__(self) -> None:
        self.vault_dir = self.vault_dir.resolve()
        if not self.vault_dir.is_dir():
            raise VaultError(f"vault directory does not exist: {self.vault_dir.name}")
        if self.foreign_vault_dir:
            foreign = self.foreign_vault_dir.resolve()
            # Guards against the target being set to -- or inside -- the
            # operator's pre-existing research vault, which the bot must never
            # write into.
            if foreign == self.vault_dir or foreign in self.vault_dir.parents:
                raise VaultError(
                    "refusing to use a vault inside the pre-existing research vault"
                )

    @property
    def target_dir(self) -> Path:
        return self.vault_dir / self.subdir

    def _resolve(self, stem: str) -> Path:
        path = (self.target_dir / f"{stem}.md").resolve()
        if self.target_dir.resolve() not in path.parents:
            raise VaultError("refusing to write outside the research subdirectory")
        return path

    def write(self, stem: str, markdown: str, *, overwrite: bool = False) -> Path:
        """Write one record atomically. Returns the path written."""
        self.target_dir.mkdir(parents=True, exist_ok=True)
        path = self._resolve(stem)

        if path.exists() and not overwrite:
            # Idempotency: a re-run of a crashed batch must not duplicate or
            # clobber. Supersession is an explicit operation, not a side effect.
            log.info("record already exists; skipping", extra={"path": path})
            return path

        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(markdown, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        log.info("record written", extra={"path": path, "bytes": len(markdown)})
        return path

    def digest(self, limit: int = 400) -> str:
        """A compact index of existing entries, for the triage stage.

        Titles and statuses only. Sending answer bodies back would balloon the
        cached prefix for no gain -- triage only needs to know what already
        exists.
        """
        if not self.target_dir.is_dir():
            return "(archive is empty)"
        lines = []
        for path in sorted(self.target_dir.glob("*.md"))[:limit]:
            status = "unknown"
            for line in path.read_text(encoding="utf-8").splitlines()[:12]:
                if line.startswith("research_status:"):
                    status = line.split(":", 1)[1].strip()
                    break
            lines.append(f"- {path.stem} [{status}]")
        return "\n".join(lines) or "(archive is empty)"


def git_commit(vault_dir: Path, message: str, *, push: bool = False) -> bool:
    """Commit whatever the run wrote. Returns False if there was nothing to do.

    Uses a neutral identity for the same reason the source repo does: members
    can read this repository, and the operator's real name and mailbox do not
    belong in its history.
    """
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(vault_dir), *args],
            capture_output=True, text=True, check=False,
        )

    if run("rev-parse", "--git-dir").returncode != 0:
        log.warning("vault is not a git repository; skipping commit")
        return False

    if not run("status", "--porcelain").stdout.strip():
        return False

    run("config", "user.name", "signal-research-bot")
    run("config", "user.email", "signal-research-bot@users.noreply.github.com")
    run("add", "-A")
    result = run("commit", "-m", message)
    if result.returncode != 0:
        log.error("vault commit failed", extra={"code": result.returncode})
        return False
    if push:
        pushed = run("push")
        if pushed.returncode != 0:
            # Not fatal: the records are committed locally and the next run
            # will push them. Failing the batch here would lose the research.
            log.warning("vault push failed; records are committed locally")
    return True
