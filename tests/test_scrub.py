"""Tests for the scrub firewall.

Every string in this file is SYNTHETIC. Real tokens live only in the gitignored
var/scrub_tokens.local. This file is in scrub_check's ALLOWLIST_PATHS precisely
so it can contain lookalike strings without tripping the checker it tests.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.scrub_check import (  # noqa: E402
    STRUCTURAL_RULES,
    scan,
    token_rules,
)

SYNTHETIC_TOKENS = ["Jordan", "jordan.k@example.invalid", "jkelly", "LAPTOP-ZZ99XX"]


def rules():
    return STRUCTURAL_RULES + token_rules(SYNTHETIC_TOKENS)


def _write(tmp_path: Path, content: str, name: str = "sample.py") -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


def check(tmp_path: Path, content: str, name: str = "sample.py"):
    findings, _pragmas = scan([_write(tmp_path, content, name)], rules())
    return findings


# --- structural rules --------------------------------------------------------


@pytest.mark.parametrize(
    "content,rule",
    [
        (r'CACHE = "C:\Users\jkelly\AppData\Local\cache"', "absolute-windows-path"),
        (r'p = "D:/Users/someone/thing"', "absolute-windows-path"),
        # Non-user absolute paths leak the operator's layout too.
        (r'vault = "D:\obsidian\CF"', "absolute-windows-path"),
        (r'out = "E:/projects/private"', "absolute-windows-path"),
        (r'home = "%USERPROFILE%\.ssh"', "user-profile-expansion"),
        ('num = "+14155550123"', "e164-phone"),
        ('aci = "3fa85f64-5717-4562-b3fc-2c963f66afa6"', "uuid"),
        ('to = "someone@example.org"', "email"),
        ("# we anonymize the transcript", "privacy-word-overclaim"),
        ("# fully anonymised output", "privacy-word-overclaim"),
    ],
)
def test_structural_rules_fire(tmp_path, content, rule):
    found = check(tmp_path, content)
    assert any(r == rule for _, _, r, _ in found), f"{rule} did not fire on: {content}"


@pytest.mark.parametrize(
    "content",
    [
        'email = "signal-research-bot@users.noreply.github.com"',  # the bot's own
        'path = Path("var") / "cache.db"',                          # relative
        'root = os.environ["SRB_DATA_DIR"]',                        # env-driven
        "# we pseudonymise the transcript",                         # correct word
        'ts = "2026-07-25T09:15:00Z"',                              # not a phone
    ],
)
def test_structural_rules_allow_legitimate_code(tmp_path, content):
    assert check(tmp_path, content) == []


# --- token rules -------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "# owner: Jordan",
        "# owner: JORDAN",          # case-insensitive
        "contact = 'jordan.k@example.invalid'",
        r'p = "C:\Users\jkelly\repo"',
        'host = "LAPTOP-ZZ99XX"',
    ],
)
def test_tokens_are_caught(tmp_path, content):
    assert check(tmp_path, content), f"token not caught in: {content}"


def test_token_substring_does_not_false_positive(tmp_path):
    """Alphanumeric tokens are word-bounded so common words stay usable."""
    # "Jordan" must not fire inside "Jordanian"; the checker would otherwise be
    # so noisy it gets disabled, which is the real failure mode.
    assert check(tmp_path, "country = 'Jordanian'") == []


def test_nfkc_lookalike_cannot_smuggle_a_token(tmp_path):
    """Fullwidth characters normalise to ASCII before matching."""
    assert check(tmp_path, "owner = 'ＪＯＲＤＡＮ'")


def test_path_separator_variant_cannot_smuggle_a_token(tmp_path):
    """A token written with '/' must also catch the '\\' spelling.

    Regression: a token listed as "obsidian/CF" silently failed to match
    "D:\\obsidian\\CF" in documentation, and only a separate grep audit caught it.
    """
    findings, _ = scan(
        [_write(tmp_path, r'vault = "D:\notes\CF"')],
        STRUCTURAL_RULES + token_rules(["notes/CF"]),
    )
    assert any(r.startswith("token:") for _, _, r, _ in findings)


# --- the scrub-ok pragma -----------------------------------------------------


def test_pragma_on_same_line_suppresses_named_rule(tmp_path):
    assert check(tmp_path, "# we anonymise it  # scrub-ok: privacy-word-overclaim") == []


def test_pragma_on_preceding_line_suppresses_named_rule(tmp_path):
    md = "<!-- scrub-ok: privacy-word-overclaim -->\nnever say anonymised\n"
    assert check(tmp_path, md, name="doc.md") == []


def test_pragma_must_name_the_rule(tmp_path):
    """A bare pragma suppresses nothing -- otherwise it becomes a blanket mute."""
    assert check(tmp_path, "# we anonymise it  # scrub-ok:") != []


def test_pragma_does_not_suppress_other_rules_on_the_same_line(tmp_path):
    """Suppressing one rule must not blind the line to everything else."""
    line = r'p = "C:\Users\jkelly\x" # anonymised # scrub-ok: privacy-word-overclaim'
    names = {r for _, _, r, _ in check(tmp_path, line)}
    assert "absolute-windows-path" in names
    assert "privacy-word-overclaim" not in names


def test_pragma_does_not_leak_two_lines_down(tmp_path):
    """Scope is the pragma's own line and the next one only."""
    md = "<!-- scrub-ok: privacy-word-overclaim -->\nfine here\nanonymised again\n"
    assert check(tmp_path, md, name="doc.md") != []


# --- fail-closed behaviour ---------------------------------------------------


def test_missing_token_file_exits_nonzero(tmp_path, monkeypatch):
    """No tokens available must be an error, never a silent pass.

    A silent pass is the worst outcome: the check appears green on a fresh
    clone or in CI while providing no protection whatsoever.
    """
    monkeypatch.delenv("SCRUB_TOKENS", raising=False)
    monkeypatch.setattr("tools.scrub_check.TOKEN_FILE", tmp_path / "absent.local")
    from tools.scrub_check import load_tokens

    with pytest.raises(SystemExit) as exc:
        load_tokens()
    assert exc.value.code != 0


def test_findings_never_echo_the_matched_secret(tmp_path):
    """Output must name the rule and location, never the offending text."""
    found = check(tmp_path, "owner = 'jordan.k@example.invalid'")
    assert found
    for rel, lineno, name, hint in found:
        blob = f"{rel}{lineno}{name}{hint}"
        assert "jordan.k@example.invalid" not in blob
        assert "jordan" not in name.lower().replace("token:jor***", "")


# --- the checker actually runs -----------------------------------------------


def test_cli_runs_against_the_real_repo():
    """--all must exit 0 on this repo. If it fails, something leaked."""
    proc = subprocess.run(
        [sys.executable, "-m", "tools.scrub_check", "--all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"scrub_check failed on the real repo:\n{proc.stdout}\n{proc.stderr}"
    )


# --- CI mode ------------------------------------------------------------------


def test_structural_only_mode_warns_loudly(capsys, monkeypatch):
    """Reduced coverage must not look identical to full coverage.

    CI has no token file. Running structural rules only is the right fallback,
    but a silent one would let a green tick imply checks that never ran.
    """
    monkeypatch.setattr(sys, "argv", ["scrub_check", "--all", "--structural-only"])
    from tools.scrub_check import main

    main()
    out = capsys.readouterr().out
    assert "WARNING" in out and "structural rules only" in out
    assert "NOT being checked" in out


def test_structural_only_still_catches_shapes(tmp_path):
    """The fallback must remain useful, not merely quiet."""
    findings, _ = scan(
        [_write(tmp_path, r'p = "C:\Users\someone\x"')], list(STRUCTURAL_RULES)
    )
    assert any(r == "absolute-windows-path" for _, _, r, _ in findings)
