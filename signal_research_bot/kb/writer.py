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
CHANGELOG_SUBDIR = "Changelog"
DASHBOARD_SUBDIR = "Dashboard"

# Directories whose contents are the bot's own bookkeeping, not archive entries.
# Offering these to triage would invite it to dedupe real research against the
# changelog, or against the index page that lists the research.
#
# Defined here rather than in the modules that write them, because `digest()`
# has to exclude them and a second copy of the string is a second thing to
# forget when one of them is renamed.
NOT_ARCHIVE = frozenset({CHANGELOG_SUBDIR, DASHBOARD_SUBDIR})

# The only directories this module will ever open for writing, whatever vault it
# is pointed at.
#
# This replaces a negative guard -- "refuse if the target is inside the
# operator's other vault" -- with a positive one, and the difference matters now
# that the target IS the operator's vault. The old check answered "is this the
# wrong vault?", which stops being answerable the moment the right answer is
# yes. This answers "is this a directory the bot owns?", which stays answerable
# and is the question that actually protects the 272 pages a human wrote.
#
# It is also the guard that survives the container. The old one compared a
# resolved host path against a resolved container path: with SRB_KB_DIR=/vault
# inside the container and SRB_FOREIGN_VAULT_DIR passed through as a raw Windows
# path, a Windows path is a single-component RELATIVE path on Linux, so it
# resolved under the working directory and matched nothing. The check had never
# fired in the only configuration that ships.
OWNED_SUBDIRS = frozenset({RESEARCH_SUBDIR, CHANGELOG_SUBDIR, DASHBOARD_SUBDIR})


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
        if self.subdir not in OWNED_SUBDIRS:
            # Refused at construction, not at write time, so there is no window
            # in which a writer exists that could reach a hand-written page.
            raise VaultError(
                f"the bot does not own '{self.subdir}'; it may only write to "
                f"{', '.join(sorted(OWNED_SUBDIRS))}"
            )
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

    def digest(self, limit: int = 600) -> str:
        """A compact index of the WHOLE vault, for the triage stage.

        Titles, statuses and topic keys only. Sending answer bodies back would
        balloon the cached prefix for no gain -- triage only needs to know what
        already exists, and which key to reproduce if it sees the same subject
        raised again.

        This used to glob `Research Log/` alone, which was right while the bot
        owned its vault and became the central defect the moment it did not. In
        a vault the operator has also been writing in by hand, one directory is
        a fraction of the archive: triage was shown five pages out of 277 and
        confidently researched five subjects the vault already covered, from
        primary sources, at Opus prices. Deduplication against an archive you
        cannot see is not deduplication.

        So it walks every content directory. Pages the bot wrote carry a
        `topic_key` and a `research_status`; hand-written pages carry neither
        and are listed with their `entity_type` instead. That difference is
        deliberate and is explained to the model in the triage prompt: a key it
        is shown is a page it may update, and an entry with no key is a page it
        may only mark as a duplicate.

        Grouped by directory because the vault's directories ARE its taxonomy,
        and a flat 277-line list buries that.

        The scan window is 20 lines rather than the frontmatter's exact height:
        at 12 it was exactly the block size, so one added key would have pushed
        a value out of range and silently degraded every entry to "unknown".
        """
        if not self.vault_dir.is_dir():
            return "(archive is empty)"

        by_dir: dict[str, list[str]] = {}
        seen = 0
        truncated = 0
        for path in sorted(self.vault_dir.rglob("*.md")):
            rel = path.relative_to(self.vault_dir)
            # Dot-directories are Obsidian's, git's and the bot's own state.
            if any(part.startswith(".") for part in rel.parts):
                continue
            if rel.parts[0] in NOT_ARCHIVE:
                continue
            if seen >= limit:
                truncated += 1
                continue
            seen += 1

            status = key = entity = ""
            for line in path.read_text(encoding="utf-8").splitlines()[:20]:
                if line.startswith("research_status:"):
                    status = line.split(":", 1)[1].strip()
                elif line.startswith("topic_key:"):
                    key = line.split(":", 1)[1].strip()
                elif line.startswith("entity_type:"):
                    entity = line.split(":", 1)[1].strip()

            if key:
                entry = f"- {path.stem} [{status or 'unknown'}] (key: {key})"
            else:
                entry = f"- {path.stem}" + (f" [{entity}]" if entity else "")
            by_dir.setdefault(str(rel.parent) if rel.parent.name else "/", []).append(entry)

        if not by_dir:
            return "(archive is empty)"
        out: list[str] = []
        for folder in sorted(by_dir):
            out.append(f"{folder}/" if folder != "/" else "(vault root)")
            out.extend(by_dir[folder])
            out.append("")
        if truncated:
            # Never silent: a truncated listing reads exactly like a complete
            # one, and the consequence here is the bot re-researching whatever
            # fell off the end.
            out.append(f"({truncated} further page(s) not listed -- index limit reached)")
        return "\n".join(out).strip()


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

    # Staged by pathspec, never `add -A`.
    #
    # `add -A` stages the vault root. That was harmless while the bot owned the
    # vault and is not harmless in the operator's own: it would stage 940
    # node_modules files, a 9.9 MB Windows executable, a settings file holding a
    # replayable log of commands that swept the operator's Downloads, Desktop
    # and Pictures, and every hand edit they had in progress -- committing all
    # of it under the bot's authorship, and pushing it.
    #
    # A .gitignore would cover the first two. It would not cover the operator's
    # unfinished work, and it is a file someone can edit. This is the version
    # that holds without anyone maintaining it.
    owned = [d for d in sorted(OWNED_SUBDIRS) if (vault_dir / d).is_dir()]
    if not owned:
        return False
    run("add", "--", *owned)

    # And then check. The pathspec above is the intent; this is the evidence.
    # A staged path outside the bot's directories means something upstream is
    # wrong, and committing anyway would be exactly the silent damage the
    # pathspec exists to prevent.
    staged = run("diff", "--cached", "--name-only").stdout.splitlines()
    stray = [p for p in staged if p and not any(p.startswith(f"{d}/") for d in owned)]
    if stray:
        log.error(
            "refusing to commit: staged paths outside the directories the bot owns",
            extra={"count": len(stray)},
        )
        run("reset")
        return False

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
