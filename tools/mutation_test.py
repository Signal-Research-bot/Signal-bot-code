#!/usr/bin/env python3
"""Prove the egress test suite would notice if the firewall were weakened.

A passing test suite says the firewall works on the inputs it was given. It
does not say the suite would *catch* a regression. This harness answers that:
it deliberately breaks one part of the firewall at a time and asserts the suite
goes red. A mutation that survives is a hole in the tests, not a pass.

That distinction is not academic here. The first version of the evasion tests
passed against a build with Unicode normalisation removed entirely -- the test
string happened to contain a second, unobfuscated roster name, so it never
exercised the path it claimed to. Only mutation testing surfaced that.

Run before any change to egress.py, and in CI:

    python -m tools.mutation_test
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EGRESS = REPO_ROOT / "signal_research_bot" / "egress.py"
CLIENT = REPO_ROOT / "signal_research_bot" / "claude" / "client.py"
SUITE = "tests/test_egress.py tests/test_client.py"


@dataclass(frozen=True)
class Mutation:
    label: str
    old: str
    new: str
    target: Path = EGRESS


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "known-identity check disabled",
        "def _assert_no_known_identity(text: str, policy: Policy, sha: str) -> None:",
        "def _assert_no_known_identity(text: str, policy: Policy, sha: str) -> None:\n    return",
    ),
    Mutation(
        "identity-shape check disabled",
        "def _assert_no_identity_shapes(text: str, sha: str) -> None:",
        "def _assert_no_identity_shapes(text: str, sha: str) -> None:\n    return",
    ),
    Mutation(
        "speaker-label check disabled",
        "def _assert_labels_are_allowed(text: str, policy: Policy, sha: str) -> None:",
        "def _assert_labels_are_allowed(text: str, policy: Policy, sha: str) -> None:\n    return",
    ),
    Mutation(
        "request-shape check disabled",
        "def _assert_request_shape(payload: Any, sha: str) -> None:",
        "def _assert_request_shape(payload: Any, sha: str) -> None:\n    return",
    ),
    Mutation(
        "unicode normalisation removed",
        'folded = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)',
        "folded = text",
    ),
    Mutation(
        "invisible-character stripping removed",
        'folded = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)',
        'folded = unicodedata.normalize("NFKC", text)',
    ),
    Mutation(
        "inspects messages only, not the whole body",
        "body = json.dumps(payload, ensure_ascii=False, sort_keys=True)",
        'body = str(payload.get("messages", ""))',
    ),
    Mutation(
        "roster phones ignored",
        "for p in self.roster.phones:",
        "for p in ():",
    ),
    # The claim these defend: there is no path to the API that skips the
    # firewall. A reviewer can read that in client.py; these prove it.
    Mutation(
        "client sends without the outbound firewall",
        "sha = guard(request, self.policy, self.quarantine_dir)",
        'sha = "unchecked"',
        CLIENT,
    ),
    Mutation(
        "client returns a response without the inbound firewall",
        "check_inbound(text, self.policy)",
        "pass",
        CLIENT,
    ),
    Mutation(
        "client reads the body before checking for a refusal",
        'if getattr(response, "stop_reason", None) == "refusal":',
        'if False and getattr(response, "stop_reason", None) == "refusal":',
        CLIENT,
    ),
)


def _run_suite() -> bool:
    """True if the suite passes."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *SUITE.split(), "-q", "--no-header", "-x"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    targets = {m.target for m in MUTATIONS}
    originals = {t: t.read_text(encoding="utf-8") for t in targets}

    if not _run_suite():
        print("baseline suite is already failing -- fix that first.", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        backups = {t: Path(tmp) / f"{i}.py" for i, t in enumerate(targets)}
        for t, b in backups.items():
            shutil.copy2(t, b)
        survivors: list[str] = []
        try:
            for m in MUTATIONS:
                source = originals[m.target]
                if m.old not in source:
                    print(f"  STALE    {m.label}: pattern no longer present")
                    survivors.append(f"{m.label} (stale pattern)")
                    continue

                m.target.write_text(source.replace(m.old, m.new, 1), encoding="utf-8")
                if _run_suite():
                    print(f"  SURVIVED {m.label}")
                    survivors.append(m.label)
                else:
                    print(f"  killed   {m.label}")
                m.target.write_text(source, encoding="utf-8")
        finally:
            for t, b in backups.items():
                shutil.copy2(b, t)

    if survivors:
        print(
            "\nMutations survived -- the egress suite would NOT catch these "
            "regressions:\n  - " + "\n  - ".join(survivors),
            file=sys.stderr,
        )
        return 1

    print(f"\nAll {len(MUTATIONS)} mutations killed. The suite has teeth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
