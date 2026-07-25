#!/usr/bin/env python3
"""Block host- and author-identifying strings from reaching either git repo.

This is the second of the project's two privacy firewalls. The first (egress.py)
protects group members from the Anthropic API; this one protects the operator and
this machine from the public repo. See PRIVACY.md and
.claude/skills/privacy-invariants.

Design constraints that are load-bearing:

* The literal strings being searched for are themselves sensitive, so they are
  NOT in this file. They live in ``var/scrub_tokens.local`` (gitignored) or come
  from the SCRUB_TOKENS environment variable. This checker is safe to publish.
* Fails closed. A missing token file is an error, not a silent pass -- otherwise
  the check quietly succeeds on a fresh clone and provides no protection at all.
* Runs in pre-commit AND CI. Pre-commit alone is bypassable with ``SKIP=``.

Usage
-----
    python -m tools.scrub_check                 # staged changes (pre-commit)
    python -m tools.scrub_check --all           # every tracked file (CI)
    python -m tools.scrub_check --paths a.py    # explicit paths
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = REPO_ROOT / "var" / "scrub_tokens.local"

# Files whose contents are allowed to describe the rules without tripping them.
# Kept deliberately tiny: every entry here is a hole in the firewall.
ALLOWLIST_PATHS = {
    "tools/scrub_check.py",
    "tests/test_scrub.py",
}

# Narrow, per-line escape hatch for documentation that must *describe* a
# forbidden pattern (PRIVACY.md explaining what a leaked path looks like, a
# skill file quoting the word we ban). Deliberately not a blanket suppression:
#
#   * it must name the exact rule -- a bare "scrub-ok" does nothing
#   * it only affects its own line, or the line immediately after it
#   * every use is greppable, and the run prints how many are in play
#
# Markdown form (invisible when rendered):  <!-- scrub-ok: rule-name -->
# Code form:                                # scrub-ok: rule-name
PRAGMA = re.compile(r"scrub-ok:\s*([a-z0-9,\s-]+)", re.I)

# Binary/large types we skip rather than decode.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mp3", ".wav", ".vhdx", ".db",
}


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    hint: str


# --- Structural rules: always on, independent of the local token file ---------
#
# These catch the *shape* of an identifying string, so they keep working even if
# the operator forgets to add a new token to their local file.

STRUCTURAL_RULES: list[Rule] = [
    # Any absolute Windows path, not just user profiles: "D:/obsidian/CF" leaks
    # the operator's unrelated private work just as surely as "C:/Users/<name>".
    Rule(
        "absolute-windows-path",
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", re.I),
        "Absolute Windows path. Use an env var with a relative default.",
    ),
    Rule(
        "user-profile-expansion",
        re.compile(r"%USERPROFILE%|\$env:USERPROFILE|~[\\/]\.claude", re.I),
        "Host-specific path expansion. Use a config value.",
    ),
    Rule(
        "e164-phone",
        re.compile(r"(?<![\w+])\+[1-9]\d{7,14}(?![\w])"),
        "Looks like a phone number in E.164 form.",
    ),
    Rule(
        "uuid",
        re.compile(
            r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
            r"-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])",
            re.I,
        ),
        "Looks like a Signal ACI/PNI UUID.",
    ),
    Rule(
        "email",
        # Deliberately excludes the users.noreply form the bot commits under.
        re.compile(
            r"(?<![\w.+-])[\w.+-]+@(?!users\.noreply\.github\.com)"
            r"[\w-]+\.[\w.-]+(?![\w.-])"
        ),
        "Email address. The bot's users.noreply address is the only one allowed.",
    ),
    # NB: this rule's own name must not contain the word it bans, or every
    # mention of the rule in documentation trips it. Learned the hard way.
    Rule(
        "privacy-word-overclaim",
        re.compile(r"\banonymi[sz](?:e|ed|es|ing|ation)\b", re.I),
        'Say "pseudonymise". Stable pseudonyms are reversible; the stronger '
        "word overstates the guarantee to non-expert readers.",
    ),
]


def _normalise(text: str) -> str:
    """Fold text so trivial variations cannot smuggle a token past us.

    * NFKC, so fullwidth and other lookalike Unicode forms collapse to ASCII.
    * Backslashes to forward slashes, so a token written "obsidian/CF" also
      matches "obsidian\\CF". Every structural rule accepts both separators.
    """
    return unicodedata.normalize("NFKC", text).replace("\\", "/")


def load_tokens() -> list[str]:
    """Load sensitive literals from the environment or the gitignored file.

    Fails closed: no tokens available is an error, never a silent pass.
    """
    def _display(p: Path) -> str:
        try:
            return p.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return p.as_posix()

    raw = os.environ.get("SCRUB_TOKENS")
    if raw:
        source = "SCRUB_TOKENS env var"
        lines = raw.splitlines()
    elif TOKEN_FILE.exists():
        source = _display(TOKEN_FILE)
        lines = TOKEN_FILE.read_text(encoding="utf-8").splitlines()
    else:
        sys.exit(
            f"scrub_check: no tokens available.\n"
            f"  Expected {_display(TOKEN_FILE)} (gitignored) or "
            f"$SCRUB_TOKENS.\n"
            f"  Refusing to run without them -- an empty token list would pass "
            f"everything.\n"
            f"  See .env.example for the expected format."
        )

    tokens = [
        _normalise(ln.strip())
        for ln in lines
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not tokens:
        sys.exit(f"scrub_check: {source} contained no tokens. Refusing to run.")
    return tokens


def token_rules(tokens: list[str]) -> list[Rule]:
    """Build case-insensitive whole-ish-word rules for each sensitive literal."""
    rules = []
    for tok in tokens:
        # \b fails next to punctuation-heavy tokens (emails, paths), so only
        # apply word boundaries to purely alphanumeric ones.
        body = re.escape(tok)
        pattern = rf"\b{body}\b" if tok.isalnum() else body
        rules.append(
            Rule(
                f"token:{tok[:3]}***",  # never echo the full token in output
                re.compile(pattern, re.I),
                "Host- or author-identifying literal from the local token list.",
            )
        )
    return rules


def git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def target_files(mode: str, explicit: list[str]) -> list[Path]:
    if explicit:
        names = explicit
    elif mode == "all":
        names = git("ls-files")
    else:
        names = git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    return [REPO_ROOT / n for n in names]


def _pragma_names(line: str) -> set[str]:
    m = PRAGMA.search(line)
    if not m:
        return set()
    # strip() must eat the trailing "-->" of an HTML comment as well as spaces,
    # or the captured name never matches a rule.
    return {n.strip(" \t-").lower() for n in m.group(1).split(",") if n.strip(" \t-")}


def _suppressed(lines: list[str], idx: int) -> set[str]:
    """Rule names suppressed for lines[idx].

    Scope is the line itself plus the nearest preceding *non-blank* line, so a
    pragma can sit above a Markdown paragraph (which is separated by a blank
    line) without silently doing nothing.
    """
    names = _pragma_names(lines[idx])
    j = idx - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j >= 0:
        names |= _pragma_names(lines[j])
    return names


def scan(
    paths: list[Path], rules: list[Rule]
) -> tuple[list[tuple[str, int, str, str]], int]:
    """Return (findings, pragma_count). Pragma count is reported for auditing."""
    findings = []
    pragmas = 0
    for path in paths:
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()  # outside the repo (e.g. a test tmp_path)
        if rel in ALLOWLIST_PATHS or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable; nothing to leak in text form

        lines = _normalise(text).splitlines()
        for idx, line in enumerate(lines):
            if PRAGMA.search(line):
                pragmas += 1
            allowed = _suppressed(lines, idx)
            for rule in rules:
                if rule.name.lower() in allowed:
                    continue
                if rule.pattern.search(line):
                    findings.append((rel, idx + 1, rule.name, rule.hint))
    return findings, pragmas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="scan every tracked file")
    ap.add_argument("--paths", nargs="*", default=[], help="explicit paths to scan")
    ap.add_argument(
        "--structural-only",
        action="store_true",
        help="skip the host-specific token list (for CI, where it does not exist). "
             "Structural rules still run; coverage is reduced and reported.",
    )
    args = ap.parse_args()

    if args.structural_only:
        # Said out loud rather than silently degraded. A run that checks fewer
        # rules must not look identical to one that checks all of them.
        print(
            "scrub_check: WARNING -- running structural rules only. The "
            "host-specific token list was not supplied, so author names, "
            "usernames and hostnames are NOT being checked."
        )
        rules = list(STRUCTURAL_RULES)
    else:
        rules = STRUCTURAL_RULES + token_rules(load_tokens())
    paths = target_files("all" if args.all else "staged", args.paths)

    if not paths:
        print("scrub_check: nothing to scan.")
        return 0

    findings, pragmas = scan(paths, rules)
    if not findings:
        note = f", {pragmas} scrub-ok pragma(s) in effect" if pragmas else ""
        print(f"scrub_check: clean ({len(paths)} file(s) scanned{note}).")
        return 0

    print("scrub_check: BLOCKED -- identifying content found.\n", file=sys.stderr)
    for rel, lineno, name, hint in findings:
        # Print the location and the rule, never the matched text itself.
        print(f"  {rel}:{lineno}  [{name}]  {hint}", file=sys.stderr)
    print(
        "\nNothing was committed. Fix the findings above; do not add entries to "
        "ALLOWLIST_PATHS to silence them.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
