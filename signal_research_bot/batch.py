"""The batch orchestrator: one window, start to finish.

Read this file to understand the pipeline. It is deliberately linear and does
no clever work of its own -- every decision it makes is delegated to a module
that is separately tested, so this stays a readable sequence of steps.

    cache -> transcript -> extract -> triage -> GATE -> cheap -> [escalate?]
          -> deep -> format -> grounding -> vault -> commit -> mark processed

Failure policy: a task that fails is skipped and counted, never retried in a
loop and never allowed to abort the window. One bad question must not cost the
other nineteen.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import datetime, timezone

from .cache import Cache, CacheEncryptionUnavailable
from .claude import stages
from .claude.client import AnthropicTransport, Client, Refusal
from .config import Config, ConfigError
from .egress import EgressViolation, Policy, check_outbound
from .gate import allowed_urls, apply_gate, reject_ungrounded, should_escalate
from .identity import (
    KeyUnavailable,
    PseudonymStore,
    Roster,
    load_observed_handles,
    load_or_create_key,
    vet_auto_handles,
)
from .kb.render import depersonalise, render, title_for
from .kb.writer import VaultError, VaultWriter, git_commit
from .logging_setup import configure
from .metrics import record_run
from .notify import Notifier, SendFailed
from .redact import RedactionUnavailable, Redactor
from .transcript import Builder

log = logging.getLogger(__name__)


def run(cfg: Config, *, dry_run: bool = False) -> int:
    roster = Roster.load(cfg.roster_path)
    pseudonyms = PseudonymStore(load_or_create_key(), cfg.pseudonyms_path)

    cache = Cache.open(cfg.cache_path, cfg.cache_key)
    messages = cache.pending()
    if not messages:
        log.info("nothing pending; window is empty")
        return 0

    # Learn what members are called by watching, rather than requiring the
    # operator to know. In a pseudonymous group they cannot supply the list, so
    # the receiver records the display name Signal attaches to each message and
    # this merges the safe ones into the deny-list automatically.
    #
    # Vetted first, because a display name is attacker-controlled and, more
    # often, simply unlucky. Anyone can call themselves "Tether"; trusting that
    # would redact the subject matter out of the archive silently, in a way that
    # looks like the research merely came up empty. See vet_auto_handles.
    #
    # Anything listed by hand in roster.json is used as-is and never vetted: a
    # human chose it, which is a stronger signal than any heuristic here.
    auto_rejected: dict[str, str] = {}
    if cfg.auto_handles:
        observed = load_observed_handles(cfg.observed_handles_path)
        if observed:
            auto, auto_rejected = vet_auto_handles(
                observed, [m.body for m in messages]
            )
            roster = replace(roster, handles=tuple({*roster.handles, *auto}))
            log.info(
                "merged observed handles into the deny-list",
                extra={"accepted": len(auto), "rejected": len(auto_rejected)},
            )
            for handle, reason in auto_rejected.items():
                # Never silent: a rejected handle means that person stays
                # visible in the transcript, and the operator can override by
                # listing it in roster.json by hand.
                log.warning(
                    "observed handle not used automatically",
                    extra={"reason": reason},
                )

    builder = Builder(roster, pseudonyms, Redactor(roster=roster))
    transcript = builder.build(messages)
    window_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # The firewall's allow-list is built from the labels actually allocated, so
    # a label the pseudonym table has never issued is treated as a violation
    # rather than passed through.
    policy = Policy.build(roster, builder.labels_in_use(), cfg.group_id)

    if dry_run:
        # Prints the exact outbound payload, writes nothing, sends nothing --
        # and runs the firewall over it, because "would this have been allowed
        # out?" is the entire question a pre-flight check exists to answer.
        request = stages.extract(transcript, cfg.research_domain)
        print(json.dumps(request, indent=2, ensure_ascii=False))
        print(f"\n-- dry run: {len(messages)} messages, nothing sent, nothing written --")
        print(json.dumps(builder.stats.as_dict(), indent=2))
        try:
            sha = check_outbound(request, policy)
        except EgressViolation as exc:
            print(f"\nEGRESS: BLOCKED [{exc.rule}] {exc.detail}", file=sys.stderr)
            return 1
        print(f"\nEGRESS: would be allowed (sha={sha})")
        return 0

    client = Client(
        policy=policy,
        quarantine_dir=cfg.quarantine_dir,
        transport=AnthropicTransport(),
    )
    vault = VaultWriter(cfg.kb_dir, cfg.foreign_vault_dir) if cfg.kb_dir else None

    stats = {
        "window": window_id,
        "messages": len(messages),
        "transcript": builder.stats.as_dict(),
        "cheap_attempted": 0, "cheap_resolved": 0, "escalated": 0,
        "written": 0, "failed": 0, "ungrounded_dropped": 0, "refusals": 0,
        "unsourced_dropped": 0,
        # What the roster actually covered. Recorded because an empty roster no
        # longer refuses to run: without this, a window with no deny-list at all
        # would look identical in the metrics to a fully covered one.
        "roster_coverage": roster.coverage(),
        "auto_handles_rejected": len(auto_rejected),
    }

    # --- stage 1 + 2 ---------------------------------------------------------
    #
    # These two calls carry the whole window, so a firewall block here fails the
    # window rather than one task. It must still be *consumed*: the messages stay
    # pending on a crash by design, but a payload the firewall rejects would be
    # rebuilt identically on the next run and rejected again, wedging every
    # future window behind it. One redaction miss would have stopped the bot
    # permanently and silently. The content is quarantined for review instead.
    try:
        extracted, _ = client.send_json(**stages.extract(transcript, cfg.research_domain))
        candidates = extracted.get("tasks") or []
        if not candidates:
            _finish(cache, window_id, messages, cfg, client, stats, vault)
            return 0

        digest = vault.digest() if vault else "(archive is empty)"
        triaged, _ = client.send_json(
            **stages.triage(candidates, digest, cfg.research_domain)
        )
    except EgressViolation as exc:
        stats["failed"] += 1
        stats["window_blocked"] = exc.rule
        log.error(
            "egress blocked the window; quarantined and skipped",
            extra={"rule": exc.rule, "window": window_id},
        )
        _finish(cache, window_id, messages, cfg, client, stats, vault)
        return 1
    except (Refusal, ValueError) as exc:
        # A refusal or unparseable structured output on stage 1/2 is transient
        # in a way a firewall block is not, so the window is NOT consumed here.
        stats["failed"] += 1
        log.error("window failed before triage", extra={"error": type(exc).__name__})
        record_run(cfg.metrics_path, stats)
        cache.close()
        return 1

    gated = apply_gate(
        triaged.get("tasks") or [],
        worth_threshold=cfg.worth_threshold,
        max_tasks=cfg.max_tasks_per_window,
    )
    stats.update(gated.counts)
    if gated.deferred_over_cap:
        # Never silent: a truncated list reads exactly like "nothing was missed".
        #
        # The count alone was not enough. The window is marked processed at the
        # end of this run, so a task deferred by the cap was never seen again --
        # the only record that a question existed was an integer in a log line.
        # The questions themselves go into metrics.jsonl, which is the file the
        # operator already reviews, so a deferred task can be re-raised rather
        # than quietly lost. They are post-redaction and post-triage, so they
        # carry no more identity than the transcript already did.
        stats["deferred_questions"] = [
            t.get("question", "") for t in gated.deferred_over_cap
        ]
        log.info("tasks deferred by the cap", extra={"count": len(gated.deferred_over_cap)})

    # --- stage 2.5 / 3 / 3b, per task ---------------------------------------
    written_entries: list[dict] = []
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for task in gated.accepted:
        question = task["question"]
        try:
            cheap = None
            # Ground truth for citations: what the SEARCH TOOL returned, not
            # what the model wrote in its own `sources` array. Accumulated
            # across every search stage this task runs.
            allowed: set[str] = set()
            try:
                cheap, _ = client.send_json(
                    **stages.cheap_research(question, task.get("rationale", ""))
                )
                allowed |= client.last_retrieved_urls
                stats["cheap_attempted"] += 1
                if cheap.get("resolved"):
                    stats["cheap_resolved"] += 1
            except (Refusal, ValueError) as exc:
                log.warning("cheap pass failed", extra={"error": type(exc).__name__})

            escalate, why = should_escalate(task, cheap)
            if escalate:
                stats["escalated"] += 1
                notes = json.dumps(cheap) if cheap else ""
                research_text, _ = client.send(
                    **stages.deep_research(question, why, notes)
                )
                # Without this the deep stage's own citations were never
                # harvested, so an escalated task had every source stripped.
                allowed |= client.last_retrieved_urls
            else:
                research_text = cheap["answer"]

            record, _ = client.send_json(
                **stages.format_record(question, research_text, allowed)
            )

            # Grounding: a citation the search never returned is a fabrication,
            # and a fabricated URL in a sourced archive is worse than no entry.
            bad = reject_ungrounded(record, allowed)
            if bad:
                stats["ungrounded_dropped"] += len(bad)
                record["evidence"] = [
                    e for e in record["evidence"] if e["url"].strip() in allowed
                ]
                log.warning("dropped ungrounded citations", extra={"count": len(bad)})

            # notify.py tells the group "every answer carries its sources". An
            # entry whose evidence list is empty -- because the search returned
            # nothing, or because every citation was ungrounded and stripped --
            # makes that false, and the renderer has a polite string for exactly
            # that case which reads like a finding rather than a failure. Better
            # to write nothing and count it.
            if not (record.get("evidence") or []):
                stats["unsourced_dropped"] = stats.get("unsourced_dropped", 0) + 1
                log.warning("no grounded sources; entry not written")
                continue

            record.setdefault("title", title_for(question, month))
            stem, markdown = render(record, first_raised=today, last_verified=today)
            if vault:
                vault.write(stem, markdown)
                stats["written"] += 1
                # Depersonalised again here rather than reusing the rendered
                # record: this text is broadcast back into the Signal group, and
                # the group is the one audience that can map a stable label onto
                # a real person immediately.
                written_entries.append(depersonalise({
                    "title": record.get("title", ""),
                    "finding": record.get("finding", "unestablished"),
                    "headline": record.get("headline", ""),
                }))
                stats.setdefault("findings", {})
                stats["findings"][record.get("finding", "unestablished")] = (
                    stats["findings"].get(record.get("finding", "unestablished"), 0) + 1
                )

        except Refusal as exc:
            stats["refusals"] += 1
            log.warning("model refused a task", extra={"category": exc.category})
            stats.setdefault("unfinished_questions", []).append(question)
        except EgressViolation as exc:
            # Already quarantined. Counted, not retried: the same input would
            # violate again.
            stats["failed"] += 1
            log.error("egress blocked a task", extra={"rule": exc.rule})
            stats.setdefault("unfinished_questions", []).append(question)
        except Exception as exc:  # noqa: BLE001 - one bad task must not end the window
            stats["failed"] += 1
            log.exception("task failed", extra={"error": type(exc).__name__})
            # Same reasoning as deferred_questions above: the window is consumed
            # at the end of this run, so without the question itself a failed
            # task leaves nothing to retry from.
            stats.setdefault("unfinished_questions", []).append(question)

    _finish(cache, window_id, messages, cfg, client, stats, vault, written_entries)
    return 0


def _finish(cache, window_id, messages, cfg, client, stats, vault, entries=()) -> None:
    if vault and stats.get("written"):
        git_commit(
            vault.vault_dir,
            f"research: {stats['written']} entries from window {window_id}",
            push=True,
        )
    # Marked only at the end: a crash mid-window leaves the messages pending so
    # the next run picks them up. Re-processing is safe -- the vault writer
    # will not overwrite an existing record.
    cache.mark_processed(window_id, messages)
    stats.update(
        input_tokens=client.usage.input_tokens,
        output_tokens=client.usage.output_tokens,
        searches=client.usage.searches,
    )
    record_run(cfg.metrics_path, stats)
    log.info("window complete", extra=stats)

    if cfg.notify:
        try:
            Notifier(cfg.signal_host, cfg.signal_port, cfg.group_id).summarise_run(
                stats, list(entries)
            )
        except SendFailed as exc:
            # Never fatal. The research is already committed; failing the window
            # over an undelivered summary would throw away the expensive part.
            log.warning("summary not delivered", extra={"error": str(exc)})
    cache.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Process one batch window.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the exact outbound payload and exit without sending or writing",
    )
    args = parser.parse_args()

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure(cfg.log_level)
    try:
        return run(cfg, dry_run=args.dry_run)
    except (CacheEncryptionUnavailable, KeyUnavailable, VaultError,
            RedactionUnavailable, FileNotFoundError) as exc:
        # These are all fail-closed refusals, not crashes. A traceback here
        # implies a bug; the message is the actionable part.
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        # An API error -- a mistyped key, a 429, a 400 from a parameter this
        # model does not accept -- reached the operator as a raw traceback, on
        # a scheduled job whose output nobody is watching. The most likely
        # first-run failure deserves a sentence, not a stack.
        name = type(exc).__name__
        if name.startswith(("APIError", "BadRequest", "Authentication",
                            "PermissionDenied", "RateLimit", "APIStatus",
                            "APIConnection", "InternalServer")):
            print(f"Anthropic API error ({name}): {exc}", file=sys.stderr)
            return 3
        raise


if __name__ == "__main__":
    raise SystemExit(main())
