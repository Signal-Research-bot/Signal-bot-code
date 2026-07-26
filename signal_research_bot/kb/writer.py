"""Write records into the knowledge-base vault, and commit them.

Three safety properties, each of which exists because the vault is a directory
of hand-curated notes that a human also edits:

* **Never touches a file it did not write.** A page is updated in place only
  when `kb/state.py` holds a hash matching the bytes on disk; anything else is
  a file a person has been in, and the update is refused rather than applied.
  Deciding that is the caller's job -- this module will replace a file when
  told to, and `overwrite=False` is the default precisely so that a caller must
  say so deliberately.
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
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

RESEARCH_SUBDIR = "Research Log"


class VaultError(RuntimeError):
    """A write was refused."""


class WriteOutcome(str, Enum):
    """What a write actually did.

    `UNCHANGED` and `COLLIDED` both mean "the file was not written", and the
    distinction between them is the point: one is a crashed batch re-running
    harmlessly, the other is two different findings competing for one filename
    and the second being discarded. Reported as one outcome, the caller either
    cries wolf on every benign re-run or stays silent on real loss.
    """

    CREATED = "created"      # nothing was there; written
    REPLACED = "replaced"    # existed, overwrite=True, written
    UNCHANGED = "unchanged"  # existed, byte-identical; nothing lost
    COLLIDED = "collided"    # existed with DIFFERENT content; NOT written


@dataclass(frozen=True)
class WriteResult:
    path: Path
    outcome: WriteOutcome

    @property
    def wrote(self) -> bool:
        return self.outcome in (WriteOutcome.CREATED, WriteOutcome.REPLACED)


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

    def write(self, stem: str, markdown: str, *, overwrite: bool = False) -> WriteResult:
        """Write one record atomically. Returns what actually happened.

        The outcome is RETURNED rather than only logged, because the caller has
        to be able to tell a write from a skip. It could not: batch.py counted
        every call as written, committed it, and announced it to the group -- so
        a page that was silently discarded was reported to the group as a new
        entry, which is how a real finding was lost once already.
        """
        self.target_dir.mkdir(parents=True, exist_ok=True)
        path = self._resolve(stem)

        if path.exists() and not overwrite:
            # Idempotency: a re-run of a crashed batch must not duplicate or
            # clobber. Supersession is an explicit operation, not a side effect.
            if path.read_text(encoding="utf-8") == markdown:
                log.info("record already present and identical", extra={"path": path})
                return WriteResult(path, WriteOutcome.UNCHANGED)
            log.error(
                "record NOT written: a different page already holds this name",
                extra={"path": path},
            )
            return WriteResult(path, WriteOutcome.COLLIDED)

        outcome = WriteOutcome.REPLACED if path.exists() else WriteOutcome.CREATED
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(markdown, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        log.info("record written", extra={"path": path, "bytes": len(markdown)})
        return WriteResult(path, outcome)

    def append(self, stem: str, markdown: str, *, header: str = "") -> WriteResult:
        """Add to the end of a file, creating it from `header` if absent.

        Atomic like write(), rather than open("a"): the vault may be open in
        Obsidian, and a partial append is visible there immediately.
        """
        self.target_dir.mkdir(parents=True, exist_ok=True)
        path = self._resolve(stem)
        existing = path.read_text(encoding="utf-8") if path.exists() else header
        outcome = WriteOutcome.REPLACED if path.exists() else WriteOutcome.CREATED
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(existing + markdown, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        log.info("appended", extra={"path": path, "bytes": len(markdown)})
        return WriteResult(path, outcome)

    def digest(self, limit: int = 400) -> str:
        """A compact index of existing entries, for the triage stage.

        Titles, statuses and topic keys only. Sending answer bodies back would
        balloon the cached prefix for no gain -- triage only needs to know what
        already exists, and which key to reproduce if it sees the same subject
        raised again.

        The scan window is 20 lines rather than the frontmatter's exact height:
        at 12 it was exactly the block size, so one added key would have pushed
        a value out of range and silently degraded every entry to "unknown".
        """
        if not self.target_dir.is_dir():
            return "(archive is empty)"
        lines = []
        for path in sorted(self.target_dir.glob("*.md"))[:limit]:
            status, key = "unknown", "-"
            for line in path.read_text(encoding="utf-8").splitlines()[:20]:
                if line.startswith("research_status:"):
                    status = line.split(":", 1)[1].strip()
                elif line.startswith("topic_key:"):
                    key = line.split(":", 1)[1].strip() or "-"
            lines.append(f"- {path.stem} [{status}] (key: {key})")
        return "\n".join(lines) or "(archive is empty)"


def git_commit(vault_dir: Path, message: str, *, push: bool = False) -> bool:
    """Commit whatever the run wrote. Returns False if there was nothing to do.

    Uses a neutral identity for the same reason the source repo does: members
    can read this repository, and the operator's real name and mailbox do not
    belong in its history.
    """
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            # `-c safe.directory` is not optional here.
            #
            # The vault is a bind mount from the Windows host, so inside the
            # container every file presents as uid 0 while the process runs as
            # uid 10002. Git refuses to operate on a repository owned by
            # someone else -- "detected dubious ownership" -- and rev-parse
            # returns non-zero, which this function read as "not a git
            # repository" and skipped the commit.
            #
            # The first live run hit exactly that: the page was written and
            # then never committed or pushed, so no member could read the thing
            # the run had just paid to produce. The warning claimed the vault
            # was not a repository, which was untrue and would have sent any
            # diagnosis in the wrong direction.
            #
            # Scoped to this one path with -c rather than a global config, so
            # it grants nothing beyond the directory the operator already
            # pointed the bot at.
            ["git", "-c", f"safe.directory={vault_dir}", "-C", str(vault_dir), *args],
            capture_output=True, text=True, check=False,
        )

    probe = run("rev-parse", "--git-dir")
    if probe.returncode != 0:
        log.warning(
            "vault is not a usable git repository; skipping commit",
            extra={"git_stderr": probe.stderr.strip()[:200]},
        )
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
