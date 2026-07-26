"""The gate: decides which tasks are allowed to cost money.

Pure functions, no I/O, so the rule that governs the entire cost model is
trivially testable and readable by someone auditing spend.

This is the biggest cost lever in the system, and it is deliberately enforced
in code rather than by prompting. Asking a model to "only research important
things" produces a model's opinion of important; a threshold and a hard cap
produce a bound.

Three rules:

* `worth >= threshold` -- banter, rhetorical questions and matters of taste
  never reach a research stage at all.
* `max_tasks_per_window` -- a hard integer cap, taking the highest-worth tasks.
* `max_updates` -- of that cap, how much may go on revisiting subjects the
  archive already covers.

Dropping a task costs nothing. Tightening the cap by one task per day saves
more than the entire cheap-research mechanism.

**Nothing is dropped silently.** Every rejected task is returned, and the
caller records the question text for the two categories where losing it costs
something: deferred by the cap, and dropped as a duplicate. Out-of-scope and
low-worth are counted only, deliberately -- they are the categories where
dropping is the whole point. A silently truncated list reads exactly like
"nothing was missed".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Escalation reasons that override the cheap pass's own verdict. Stage 2 sees
# the conversation and the KB; stage 2.5 sees only its own search results.
ALWAYS_ESCALATE_DIFFICULTY = frozenset({"high"})


@dataclass(frozen=True)
class GateResult:
    accepted: list[dict[str, Any]]
    rejected_out_of_scope: list[dict[str, Any]]
    rejected_low_worth: list[dict[str, Any]]
    rejected_duplicate: list[dict[str, Any]]
    deferred_over_cap: list[dict[str, Any]]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "accepted": len(self.accepted),
            "accepted_updates": sum(1 for t in self.accepted if t.get("is_update")),
            "rejected_out_of_scope": len(self.rejected_out_of_scope),
            "rejected_low_worth": len(self.rejected_low_worth),
            "rejected_duplicate": len(self.rejected_duplicate),
            "deferred_over_cap": len(self.deferred_over_cap),
        }


def apply_gate(
    tasks: Iterable[dict[str, Any]],
    *,
    worth_threshold: float,
    max_tasks: int,
    max_updates: int = 2,
) -> GateResult:
    """Split triaged tasks into what gets researched and what does not.

    A lead about a subject the archive already covers is an UPDATE rather than a
    new entry, and only when triage says the conversation carried something new.
    Three properties keep that from becoming an unbounded spend channel:

    * an update clears the same worth threshold and the same cap as a fresh lead
    * a pure restatement is still dropped for free, exactly as before
    * at equal worth a new subject outranks an update, and `max_updates` bounds
      how much of one window's cap updates may take
    """
    duplicates: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    low_worth: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for task in tasks:
        # Scope first: an off-topic lead is dropped before worth is even
        # considered, because a fascinating question about the wrong subject
        # still does not belong in this archive.
        if not task.get("in_scope", True):
            out_of_scope.append(task)
            continue
        known = bool(task.get("duplicate_of") or task.get("topic_key"))
        if known and not str(task.get("new_information") or "").strip():
            duplicates.append(task)
            continue
        if float(task.get("worth", 0.0)) < worth_threshold:
            low_worth.append(task)
            continue
        candidates.append({**task, "is_update": known} if known else task)

    # Highest worth first; at equal worth a new subject beats an update, so a
    # busy window cannot spend its whole cap revisiting what is already written.
    # Then stable by question, so a re-run of the same window makes the same
    # decisions -- the batch has to be idempotent.
    candidates.sort(
        key=lambda t: (
            -float(t.get("worth", 0.0)),
            bool(t.get("is_update")),
            t.get("question", ""),
        )
    )

    accepted: list[dict[str, Any]] = []
    over_cap: list[dict[str, Any]] = []
    updates_taken = 0
    for task in candidates:
        if len(accepted) >= max_tasks:
            over_cap.append(task)
        elif task.get("is_update") and updates_taken >= max_updates:
            over_cap.append(task)
        else:
            updates_taken += bool(task.get("is_update"))
            accepted.append(task)

    return GateResult(
        accepted=accepted,
        rejected_out_of_scope=out_of_scope,
        rejected_low_worth=low_worth,
        rejected_duplicate=duplicates,
        deferred_over_cap=over_cap,
    )


def should_escalate(triaged: dict[str, Any], cheap: dict[str, Any] | None) -> tuple[bool, str]:
    """Decide whether a task goes to the expensive model. Returns (escalate, why).

    Biased toward escalating. Haiku is materially weaker at adversarial source
    evaluation -- the exact skill this project's standard depends on -- so a
    confident-but-wrong cheap answer is the failure mode to design against,
    not cost.
    """
    if cheap is None:
        return True, "cheap pass failed or returned nothing"

    if triaged.get("difficulty") in ALWAYS_ESCALATE_DIFFICULTY:
        return True, "triage marked difficulty high"

    if not cheap.get("resolved"):
        return True, cheap.get("escalation_reason") or "cheap pass did not resolve"

    confidence = cheap.get("confidence")
    if confidence in {"single-source", "unverified"}:
        return True, f"cheap answer only reached {confidence}"

    sources = cheap.get("sources") or []
    if len(sources) < 2 and confidence != "primary":
        return True, "fewer than two sources and not primary"

    return False, ""


def allowed_urls(*results: dict[str, Any] | None) -> set[str]:
    """URLs actually retrieved by a search stage.

    The formatting stage may cite only these. A model asked to produce
    citations will otherwise produce plausible ones, and a fabricated URL in a
    sourced archive is worse than no entry -- it launders a guess into
    something that looks verified.
    """
    urls: set[str] = set()
    for result in results:
        for source in (result or {}).get("sources") or []:
            url = (source or {}).get("url")
            if url:
                urls.add(url.strip())
    return urls


def reject_ungrounded(record: dict[str, Any], allowed: set[str]) -> list[str]:
    """Return evidence URLs that were never retrieved. Empty list means clean."""
    return [
        item.get("url", "")
        for item in record.get("evidence") or []
        if item.get("url", "").strip() not in allowed
    ]
