"""What the bot knows about each topic page it has written.

A page is a living document now: research on a topic already in the archive
updates that page rather than opening a second one. Doing that needs the prior
state of the page, and this module is where it comes from.

WHY A SIDECAR AND NOT THE PAGE ITSELF
-------------------------------------
The obvious source is the page. It was rejected. Reading it back means parsing
YAML this module wrote *and* YAML a human wrote -- `writer.py` opens with the
fact that a person edits these files, and Obsidian's property editor emits block
sequences, folded scalars and unquoted colons that a hand-rolled inverter of
`_yaml_str` would not error on, it would simply read a wrong value and then
write that wrong value into a permanent page. Silent corruption of
hostile-adjacent input is the failure class this codebase has been burned by
more than once.

So the page is never read. It is a pure projection of the state below, rendered
by `render()` -- which means an update still passes through `depersonalise`,
the property an in-place line patch would have quietly given up.

WHY INSIDE THE VAULT
--------------------
`<vault>/Research Log/.srb-state/<topic_key>.json`, not `var/`:

* One `git revert` restores page, sidecar and changelog as a consistent triple.
  State outside the vault survives a rollback and then describes a page that no
  longer exists.
* It survives a wiped `var/` and travels with a clone.
* `tools/scrub_check.py --vault` globs the whole tree, so it is inside the
  operator-protecting firewall by default rather than by remembering.
* Obsidian ignores dot-directories: it never appears in the explorer, the graph
  or search.

One file per topic, so two topics touched in the same run never contend and a
corrupted file costs one topic rather than the index.

`content_sha` is the hand-edit guard. `.claude/skills/kb-schema` says the bot
never edits a human-authored page, and that line is NOT being relaxed by any of
this: before an update the file on disk is hashed, and if it does not match what
this module last wrote, a person (or a crashed half-write) has been in it. The
update is then refused and reported rather than applied.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from .render import depersonalise

log = logging.getLogger(__name__)

STATE_SUBDIR = ".srb-state"

# Strongest first. An update never weakens a page: evidence already gathered
# does not stop existing because a later pass found less of it, and a genuine
# conflict surfaces as research_status: contested, not as a quiet downgrade.
_CONFIDENCE_ORDER = ("primary", "corroborated", "single-source", "unverified")

# Sticky first. A contested claim must stay flagged, and a page must not regress
# to "open" because one re-check timed out.
_STATUS_ORDER = ("dropped", "contested", "answered", "researching", "open")


def content_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _tuple_of_dicts(value: Any) -> tuple[dict[str, Any], ...]:
    return tuple(v for v in (value or []) if isinstance(v, dict))


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in (value or []))


@dataclass(frozen=True)
class TopicState:
    """Everything needed to re-render a page, plus how it was last left."""

    topic_key: str
    stem: str                 # the filename, minus .md. Frozen at creation.
    title: str                # the H1 and frontmatter title. Frozen at creation.
    first_raised: str = ""
    last_verified: str = ""
    question: str = ""
    answer: str = ""
    headline: str = ""
    research_status: str = "open"
    confidence: str = "unverified"
    finding: str = "unestablished"
    tags: tuple[str, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    contradictions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    updates: tuple[dict[str, Any], ...] = ()
    content_sha: str = ""
    # False for a page adopted from before this module existed. The bot has no
    # record of what is in it, so it must never re-render it -- see adopt.py.
    managed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_key": self.topic_key, "stem": self.stem, "title": self.title,
            "first_raised": self.first_raised, "last_verified": self.last_verified,
            "question": self.question, "answer": self.answer,
            "headline": self.headline, "research_status": self.research_status,
            "confidence": self.confidence, "finding": self.finding,
            "tags": list(self.tags), "evidence": [dict(e) for e in self.evidence],
            "contradictions": list(self.contradictions),
            "open_questions": list(self.open_questions),
            "updates": [dict(u) for u in self.updates],
            "content_sha": self.content_sha, "managed": self.managed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TopicState":
        return cls(
            topic_key=str(raw.get("topic_key", "")),
            stem=str(raw.get("stem", "")),
            title=str(raw.get("title", "")),
            first_raised=str(raw.get("first_raised", "")),
            last_verified=str(raw.get("last_verified", "")),
            question=str(raw.get("question", "")),
            answer=str(raw.get("answer", "")),
            headline=str(raw.get("headline", "")),
            research_status=str(raw.get("research_status", "open")),
            confidence=str(raw.get("confidence", "unverified")),
            finding=str(raw.get("finding", "unestablished")),
            tags=_tuple_of_str(raw.get("tags")),
            evidence=_tuple_of_dicts(raw.get("evidence")),
            contradictions=_tuple_of_str(raw.get("contradictions")),
            open_questions=_tuple_of_str(raw.get("open_questions")),
            updates=_tuple_of_dicts(raw.get("updates")),
            content_sha=str(raw.get("content_sha", "")),
            managed=bool(raw.get("managed", True)),
        )

    def as_record(self) -> dict[str, Any]:
        """The shape render() expects, rebuilt from state."""
        return {
            "title": self.title, "question": self.question, "answer": self.answer,
            "headline": self.headline, "research_status": self.research_status,
            "confidence": self.confidence, "finding": self.finding,
            "tags": list(self.tags), "evidence": [dict(e) for e in self.evidence],
            "contradictions": list(self.contradictions),
            "open_questions": list(self.open_questions),
        }


def _union(existing: Iterable[str], incoming: Iterable[str]) -> tuple[str, ...]:
    """Existing order first, then what is new. Exact-string dedupe."""
    out = list(existing)
    seen = set(out)
    for item in incoming:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _union_evidence(
    existing: Iterable[dict[str, Any]], incoming: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Keyed on (url, quote). The evidence table is the archive's spine: a row
    dropped because a later search did not re-find it is evidence destroyed."""
    out = [dict(e) for e in existing]
    seen = {(e.get("url", ""), e.get("quote", "")) for e in out}
    for item in incoming:
        key = (item.get("url", ""), item.get("quote", ""))
        if key not in seen:
            seen.add(key)
            out.append(dict(item))
    return tuple(out)


def _strongest(a: str, b: str, order: tuple[str, ...]) -> str:
    ranked = [v for v in order if v in (a, b)]
    return ranked[0] if ranked else (b or a)


def merge(state: TopicState, record: dict[str, Any], *, today: str) -> tuple[TopicState, dict]:
    """Fold new research into an existing topic. Returns (state, update-entry).

    What is preserved, replaced and unioned is the whole design, so it is spelt
    out rather than left to be inferred:

    * `stem`, `title`, `topic_key`, `first_raised` are **preserved**. The
      filename is the wikilink target and renaming it breaks inbound links; the
      H1, the frontmatter title and the filename have to agree, so a later
      differently-phrased title is discarded. `first_raised` records when the
      group first raised it, which cannot change.
    * `last_verified` is **replaced**. It is the field the update exists for.
    * `sources`/`evidence`, `tags`, `contradictions`, `open_questions` are
      **unioned**. A recorded source or an unanswered question does not stop
      being true because a later pass did not repeat it.
    * `research_status` and `confidence` take the **stronger** value, so a
      contested claim stays flagged and a thinner re-check cannot quietly
      downgrade a primary-sourced page.
    * `answer`, `headline` and `finding` are **replaced**: the newest assessment
      is the current one. A changed finding is stated verbatim in the update
      entry -- averaging a reversal into "mixed" would launder it.
    """
    # Same reason as state_from_record: an update's prose goes into the sidecar
    # as well as onto the page, and only the page was ever stripped.
    record = depersonalise(record)
    changes: list[str] = []

    finding = str(record.get("finding", state.finding))
    if finding != state.finding:
        changes.append(f"Finding changed: {state.finding} → {finding}.")

    confidence = _strongest(
        state.confidence, str(record.get("confidence", state.confidence)),
        _CONFIDENCE_ORDER,
    )
    if confidence != state.confidence:
        changes.append(f"Confidence: {state.confidence} → {confidence}.")

    status = _strongest(
        state.research_status, str(record.get("research_status", state.research_status)),
        _STATUS_ORDER,
    )
    if status != state.research_status:
        changes.append(f"Status: {state.research_status} → {status}.")

    evidence = _union_evidence(state.evidence, record.get("evidence") or [])
    known = {e.get("url", "") for e in state.evidence}
    new_sources = [
        e.get("url", "") for e in evidence if e.get("url", "") and e.get("url") not in known
    ]

    open_questions = _union(state.open_questions, record.get("open_questions") or [])
    contradictions = _union(state.contradictions, record.get("contradictions") or [])
    if len(contradictions) > len(state.contradictions):
        changes.append(
            f"{len(contradictions) - len(state.contradictions)} new contradiction(s) recorded."
        )

    update = {
        "date": today,
        "headline": str(record.get("headline") or "").strip(),
        "changes": changes,
        "sources": new_sources,
    }

    merged = replace(
        state,
        last_verified=today,
        answer=str(record.get("answer", state.answer)),
        headline=str(record.get("headline", state.headline)),
        finding=finding,
        confidence=confidence,
        research_status=status,
        tags=_union(state.tags, [str(t) for t in (record.get("tags") or [])]),
        evidence=evidence,
        contradictions=contradictions,
        open_questions=open_questions,
        updates=state.updates + (update,),
    )
    return merged, update


def state_from_record(
    record: dict[str, Any], *, topic_key: str, stem: str, title: str, today: str
) -> TopicState:
    """The state of a page being created for the first time."""
    # Depersonalised HERE, not only in render().
    #
    # render() strips speaker labels from a copy of the record on its way to the
    # page. Nothing stripped the record on its way into the sidecar -- and the
    # sidecars live inside the vault by design, so "Participant B was wrong
    # about the reserves" would be committed and pushed to a repository the
    # group can read, in a file holding a stable per-person label attached to a
    # claim. That is precisely the harm depersonalise exists to prevent, and it
    # was reaching the repo by the one path that never called it.
    record = depersonalise(record)
    return TopicState(
        topic_key=topic_key, stem=stem, title=title,
        first_raised=today, last_verified=today,
        question=str(record.get("question", "")),
        answer=str(record.get("answer", "")),
        headline=str(record.get("headline", "")),
        research_status=str(record.get("research_status", "open")),
        confidence=str(record.get("confidence", "unverified")),
        finding=str(record.get("finding", "unestablished")),
        tags=_tuple_of_str(record.get("tags")),
        evidence=_tuple_of_dicts(record.get("evidence")),
        contradictions=_tuple_of_str(record.get("contradictions")),
        open_questions=_tuple_of_str(record.get("open_questions")),
    )


@dataclass
class VaultIndex:
    """Every topic the bot has written, keyed on topic_key."""

    target_dir: Path
    _by_key: dict[str, TopicState] = field(default_factory=dict)

    @property
    def state_dir(self) -> Path:
        return self.target_dir / STATE_SUBDIR

    def load(self) -> "VaultIndex":
        self._by_key = {}
        if not self.state_dir.is_dir():
            return self
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # One unreadable sidecar must not take the archive down with
                # it. The topic reverts to unknown, so the next run treats it
                # as new and the collision guard in writer.write() stops it
                # overwriting the page that is already there.
                log.error("unreadable topic state; ignoring", extra={"stem": path.stem})
                continue
            if isinstance(raw, dict) and raw.get("topic_key"):
                state = TopicState.from_dict(raw)
                self._by_key[state.topic_key] = state
        return self

    def get(self, topic_key: str) -> TopicState | None:
        return self._by_key.get(topic_key)

    def keys(self) -> set[str]:
        return set(self._by_key)

    def stems(self) -> set[str]:
        return {s.stem for s in self._by_key.values()}

    def all(self) -> list[TopicState]:
        return sorted(self._by_key.values(), key=lambda s: s.topic_key)

    def stage(self, state: TopicState) -> None:
        """Record in memory only. `put` is what makes it durable.

        Exists so a dry run can compute exactly what a real run would write.
        `related_stems` reads the whole index, so a plan built against an index
        that has not seen the other pages' recovered tags prints a different
        answer to the one the real run produces -- which is the one way a dry
        run can be worse than no dry run at all.
        """
        self._by_key[state.topic_key] = state

    def put(self, state: TopicState) -> None:
        self.stage(state)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"{state.topic_key}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.as_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)   # atomic: never a half-written topic

    def reserve_stem(self, preferred: str) -> str:
        """A filename no other topic already owns.

        Two genuinely different topics can condense to the same title. Without
        this the second one silently loses -- which is exactly the incident the
        write-outcome reporting exists to catch, and this stops it happening at
        all.
        """
        taken = self.stems()
        if preferred not in taken:
            return preferred
        for n in range(2, 100):
            candidate = f"{preferred} ({n})"
            if candidate not in taken:
                return candidate
        return f"{preferred} ({len(taken) + 1})"

    def related_stems(
        self, topic_key: str, tags: Iterable[str], *, limit: int = 5, min_overlap: int = 2
    ) -> list[str]:
        """Wikilink targets for topics sharing at least `min_overlap` tags.

        Computed from the index rather than asked of a model: deterministic, free,
        and incapable of inventing a page that does not exist.

        `signal-derived` and `research` are excluded because render() puts them
        on every page -- counting them, every page would link to every page.

        Nothing is written back to the target. Obsidian derives the reverse edge
        in its backlinks pane, and rewriting pages outside the run being
        performed would widen the blast radius for no gain.
        """
        mine = {t for t in tags} - {"signal-derived", "research"}
        if len(mine) < min_overlap:
            return []
        scored = []
        for state in self._by_key.values():
            if state.topic_key == topic_key:
                continue
            overlap = len(mine & (set(state.tags) - {"signal-derived", "research"}))
            if overlap >= min_overlap:
                scored.append((-overlap, state.stem))
        scored.sort()
        return [f"[[{stem}]]" for _, stem in scored[:limit]]
