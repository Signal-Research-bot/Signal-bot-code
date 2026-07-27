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
# The pre-launch audit found leaks in modules upstream of the firewall, so the
# mutations below cover those too. The suite has to widen with them: a mutation
# in transcript.py that no test in SUITE exercises would "survive" for the
# uninteresting reason that nothing ran against it.
TRANSCRIPT = REPO_ROOT / "signal_research_bot" / "transcript.py"
ENVELOPE = REPO_ROOT / "signal_research_bot" / "envelope.py"
RENDER = REPO_ROOT / "signal_research_bot" / "kb" / "render.py"
IDENTITY = REPO_ROOT / "signal_research_bot" / "identity.py"
REDACT = REPO_ROOT / "signal_research_bot" / "redact.py"
RECEIVER = REPO_ROOT / "signal_research_bot" / "receiver.py"
IMPORTER = REPO_ROOT / "signal_research_bot" / "importer.py"
WRITER = REPO_ROOT / "signal_research_bot" / "kb" / "writer.py"
BATCH = REPO_ROOT / "signal_research_bot" / "batch.py"
STATE = REPO_ROOT / "signal_research_bot" / "kb" / "state.py"
GATE = REPO_ROOT / "signal_research_bot" / "gate.py"
REPAIR = REPO_ROOT / "signal_research_bot" / "kb" / "repair.py"
SUITE = (
    "tests/test_egress.py tests/test_client.py tests/test_transcript.py "
    "tests/test_envelope.py tests/test_redact.py tests/test_kb.py "
    "tests/test_batch.py tests/test_identity.py tests/test_receiver.py "
    "tests/test_importer.py tests/test_kb_state.py tests/test_kb_migration.py "
    "tests/test_gate.py tests/test_kb_repair.py"
)


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
    # --- controls added by the pre-launch audit ------------------------------
    #
    # Each reverts one audit fix. The fix is not the deliverable; a test that
    # would notice the fix being undone is.
    Mutation(
        "bare-digit phone rule removed from the firewall",
        "for match in BARE_DIGIT_RUN_RE.finditer(text):",
        "for match in ():",
    ),
    Mutation(
        "phone validity always false (bare numbers pass)",
        "if is_dialable(match.group(1)):",
        "if False and is_dialable(match.group(1)):",
    ),
    Mutation(
        "phone validity always true (reserves figures blocked)",
        "if is_dialable(match.group(1)):",
        "if True or is_dialable(match.group(1)):",
    ),
    Mutation(
        "non-ascii digit folding removed",
        "if ch.isdigit() and not ch.isascii():",
        "if False:",
    ),
    # --- links: the class PRIVACY.md promises always leaves, and the ground
    # truth that decides what anything downstream may act on -----------------
    Mutation(
        "profile links without a scheme pass through",
        "        if SCHEMELESS_PERSONAL_RE.search(text):",
        "        if False:",
        REDACT,
    ),
    Mutation(
        "a URL a later layer rewrote is still offered as ground truth",
        "            kept_url_list=tuple(u for u in kept if u in out),",
        "            kept_url_list=tuple(kept),",
        REDACT,
    ),
    Mutation(
        "redaction telemetry from quoted text is discarded",
        "            self._count(quoted)",
        "            pass",
        TRANSCRIPT,
    ),
    Mutation(
        "opt-out ignores who is being quoted",
        "if msg.quote_text and not is_opted_out(self.roster, msg.quote_author):",
        "if msg.quote_text:",
        TRANSCRIPT,
    ),
    Mutation(
        "prompt-structure tokens passed through verbatim",
        'return f"{speaker}: {prefix}{self._defang(text)}{suffix}"',
        'return f"{speaker}: {prefix}{text}{suffix}"',
        TRANSCRIPT,
    ),
    Mutation(
        "mention surrogate guard removed",
        "if splits_surrogate(lo) or splits_surrogate(hi):",
        "if False:",
        ENVELOPE,
    ),
    Mutation(
        "negative mention start/length accepted",
        "if m.length < 0 or m.start < 0:",
        "if False:",
        ENVELOPE,
    ),
    Mutation(
        "envelope-level edits dropped again",
        'edit = envelope.get("editMessage")',
        "edit = None",
        ENVELOPE,
    ),
    Mutation(
        "frontmatter title interpolated raw",
        'f"title: {_yaml_str(title)}",',
        'f"title: {title}",',
        RENDER,
    ),
    # --- research that was researched but never filed -------------------------
    # The claim these defend: the batch counts, commits and announces only the
    # pages that actually reached the vault.
    Mutation(
        "the writer cannot tell a skip from a write",
        "            return WriteResult(path, WriteOutcome.COLLIDED)",
        "            return WriteResult(path, WriteOutcome.CREATED)",
        WRITER,
    ),
    Mutation(
        "a distinct record silently overwrites the page already there",
        "        if path.exists() and not overwrite:",
        "        if False:",
        WRITER,
    ),
    Mutation(
        "an unwritten record is counted and announced again",
        "    if not result.wrote:",
        "    if False:",
        BATCH,
    ),
    # --- one page per topic, and nobody else's page touched -------------------
    Mutation(
        "an update overwrites a page a human has edited",
        "    if state is not None and not _page_is_ours(vault, state):",
        "    if False:",
        BATCH,
    ),
    Mutation(
        "an adopted page is re-rendered from a record the bot never had",
        "    if state is not None and not state.managed:",
        "    if False:",
        BATCH,
    ),
    Mutation(
        "an update renames the page and breaks every inbound wikilink",
        "        stem, updates = new_state.stem, new_state.updates",
        "        stem, updates = slug(str(record.get('title'))), new_state.updates",
        BATCH,
    ),
    Mutation(
        "triage is allowed to mint a topic key it never saw",
        "    if matched and matched in known:",
        "    if matched:",
        BATCH,
    ),
    Mutation(
        "update blocks bypass depersonalisation",
        "    update = depersonalise(update)",
        "    update = dict(update)",
        RENDER,
    ),
    Mutation(
        "related links and updates bypass depersonalisation",
        "    payload = depersonalise({",
        "    payload = ({",
        RENDER,
    ),
    Mutation(
        "topic keys are used exactly as the model wrote them",
        "    text = depersonalise(str(value or \"\"))",
        "    text = str(value or \"\")",
        RENDER,
    ),
    Mutation(
        "last_verified is rewritten in a page whose shape we do not recognise",
        "    if len(hits) != 1:",
        "    if False:",
        RENDER,
    ),
    Mutation(
        "evidence already gathered is replaced instead of unioned",
        "    evidence = _union_evidence(state.evidence, record.get(\"evidence\") or [])",
        "    evidence = _tuple_of_dicts(record.get(\"evidence\"))",
        STATE,
    ),
    Mutation(
        "a contested finding stops being flagged after a clean re-check",
        "    status = _strongest(",
        "    status = str(record.get('research_status', state.research_status)) or _strongest(",
        STATE,
    ),
    Mutation(
        "a restatement with nothing new is researched again anyway",
        '        if known and not str(task.get("new_information") or "").strip():',
        "        if False:",
        GATE,
    ),
    Mutation(
        "an update skips the worth threshold",
        '        if float(task.get("worth", 0.0)) < worth_threshold:',
        "        if False:",
        GATE,
    ),
    # --- repairing pages the bot wrote but kept no record of ------------------
    #
    # Repair is the one path that opens a legacy page for writing. Each of these
    # reverts a guard that decides whether it may.
    Mutation(
        "a page edited since the bot wrote it is repaired anyway",
        "    if content_hash(text) != state.content_sha and not _carries_our_key(",
        "    if False and content_hash(text) != state.content_sha and not _carries_our_key(",
        REPAIR,
    ),
    Mutation(
        "a crashed repair wedges the page forever instead of re-running",
        "def _carries_our_key(lines: list[str], span: tuple[int, int], topic_key: str) -> bool:",
        "def _carries_our_key(lines: list[str], span: tuple[int, int], topic_key: str) -> bool:\n    return False",
        REPAIR,
    ),
    Mutation(
        "an unrecognised tags line is read as empty instead of refused",
        "def _parse_tags(value: str) -> tuple[str, ...] | None:",
        "def _parse_tags(value: str) -> tuple[str, ...] | None:\n    return ()",
        REPAIR,
    ),
    Mutation(
        "a topic key belonging to another topic is reassigned",
        "        if lines[hits[0]] != key_line:",
        "        if False:",
        REPAIR,
    ),
    Mutation(
        "related is computed before the other pages' tags are known",
        "        index.stage(replace(index.get(p.topic_key), tags=p.tags, **p.facts))",
        "        index.stage(replace(index.get(p.topic_key), tags=(), **p.facts))",
        REPAIR,
    ),
    # --- keeping participants out of the research itself ---------------------
    #
    # The record used to be depersonalised on its own line. It is now one key of
    # the payload dict, alongside `related` and `updates` -- so the mutation that
    # kills that call ("related links and updates bypass depersonalisation")
    # covers the page body too, and a second one here would only be the same
    # test twice.
    Mutation(
        "@handles no longer stripped from pages",
        "    return _AT_HANDLE.sub(MEMBER, _SPEAKER.sub(MEMBER, text))",
        "    return _SPEAKER.sub(MEMBER, text)",
        RENDER,
    ),
    Mutation(
        "depersonalise does not recurse into lists",
        "    if isinstance(value, list):",
        "    if False:",
        RENDER,
    ),
    Mutation(
        "handle-shaped tags are no longer filtered",
        "    supplied = [t for t in (record.get(\"tags\") or []) if _TAG_SAFE.fullmatch(str(t))]",
        "    supplied = list(record.get(\"tags\") or [])",
        RENDER,
    ),
    Mutation(
        "chat handles leak into the egress policy (wedges every window)",
        "                sorted(roster.name_variants(), key=len, reverse=True)",
        "                sorted(roster.redaction_variants(), key=len, reverse=True)",
    ),
    Mutation(
        "handles are split on whitespace like real names",
        "        for handle in self.handles:",
        "        for handle in [p for h in self.handles for p in h.split()]:",
        IDENTITY,
    ),
    Mutation(
        "redaction ignores handles entirely",
        "        return self.name_variants() | self.handle_variants()",
        "        return self.name_variants()",
        IDENTITY,
    ),
    Mutation(
        "observed handles are trusted without vetting",
        "            if rate > AUTO_HANDLE_MAX_HIT_RATE:",
        "            if False:",
        IDENTITY,
    ),
    Mutation(
        "jsonl import reads items from every conversation on the account",
        '        if str(_first(item, ("chatId",), ("chat_id",)) or "") not in chats_in_group:',
        "        if False:",
        IMPORTER,
    ),
    Mutation(
        "inspect leaks values instead of key names",
        "                shapes.setdefault(kind, set()).update(payload.keys())",
        "                shapes.setdefault(kind, set()).update(str(v) for v in payload.values())",
        IMPORTER,
    ),
    Mutation(
        "import reads rows from every conversation on the account",
        '        if row.get(thread_col) not in threads:',
        '        if False:',
        IMPORTER,
    ),
    Mutation(
        "imported outgoing messages get a second participant label",
        "            source = SELF          # matches how the live receiver labels own messages",
        "            source = acis.get(row.get(from_col, \"\"), \"\")",
        IMPORTER,
    ),
    Mutation(
        "hex group ids silently decoded as base64 (wrong group)",
        '    if re.fullmatch(r"[0-9a-fA-F]+", text) and len(text) % 2 == 0:',
        "    if False:",
        IMPORTER,
    ),
    Mutation(
        "remotely deleted messages resurrected by the import",
        '        if deleted_col and str(row.get(deleted_col, "")).strip() in ("1", "true", "True"):',
        "        if False:",
        IMPORTER,
    ),
    Mutation(
        "display names harvested before the group filter",
        "        self._observe_handle(env)",
        "        pass  # observation moved earlier",
        RECEIVER,
    ),
    Mutation(
        "every observed handle is rejected (learning does nothing)",
        "        accepted.append(handle)",
        "        rejected[raw] = 'disabled'",
        IDENTITY,
    ),
    # --- pre-launch review: the compound data-loss defect --------------------
    Mutation(
        "phone shape blocks a window without checking it IS a phone",
        "        if looks_like_phone(match.group()):",
        "        if len(_digits(match.group())) >= 9:",
    ),
    Mutation(
        "source URLs are depersonalised along with prose",
        "            parts.append(match.group())",
        "            parts.append(_scrub(match.group()))",
        RENDER,
    ),
    Mutation(
        "depersonalised titles collide on one filename again",
        "    if MEMBER in title:",
        "    if False:",
        RENDER,
    ),
    Mutation(
        "every model gets the dynamic-filtering search tool",
        "    return WEB_SEARCH_TOOL if model in DYNAMIC_FILTER_MODELS else WEB_SEARCH_TOOL_BASIC",
        "    return WEB_SEARCH_TOOL",
        CLIENT,
    ),
    Mutation(
        "effort sent to every model including Haiku",
        "    if model in EFFORT_MODELS:",
        "    if True:",
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
