"""The five cascade stages, as prompts plus request construction.

Model and parameter choices are documented in
.claude/skills/claude-cascade/SKILL.md; the reasoning is not repeated here.
The prompts themselves encode the operator's existing research standard, which
lives in .claude/skills/investigate-loop/SKILL.md: primary sources first,
triangulation, and fact kept separate from inference kept separate from
speculation.

A note on identity, corrected after the group turned out to be pseudonymous.

This docstring used to say the prompts had "no identities to leak", because the
transcript carries only "Participant A" labels and the egress firewall is the
control. That holds for real names. It missed two things:

* The labels are STABLE for the life of the archive. `Participant B` is the
  same person in every entry, so an attributed claim in a permanent,
  member-readable page identifies someone to the eight people best placed to
  work out who. The firewall cannot help here -- it *allows* allocated labels,
  by design, and a test asserts that it does.
* Members go by chat handles, which arrive as ordinary words. The firewall's
  name rule only knows what is in the roster, so a handle nobody listed is
  invisible to it.

Hence the instruction below not to refer to speakers at all. It is a soft
control and is not relied on: `kb/render.py:depersonalise` strips speaker
references from the entire record before a page is written, and again before
anything is posted back to the group. The prompt exists to make that strip a
no-op in the normal case, not to be the guarantee.
"""

from __future__ import annotations

from typing import Any

from . import schemas
from .client import HAIKU, OPUS, SONNET, search_request, structured_request

# --- stage 1: extract ---------------------------------------------------------

EXTRACT_SYSTEM = """\
You read a pseudonymised group chat transcript and surface RESEARCHABLE LEADS. \
Participants are labelled "Participant A" and so on; that is all you know \
about them and all you need.

Nobody is addressing you. This is ordinary conversation that happens to be \
observed. Do not expect questions to be well formed, directed at you, or even \
phrased as questions -- most of the best leads are not.

A lead is anything CHECKABLE against sources. All four of these count:

1. An explicit question. "Is the Q1 attestation an actual audit?"
2. An asserted claim stated as fact. "They quietly took a stake through a \
subsidiary last year." Restate it as the question that would verify it.
3. A named entity, filing, transaction or relationship raised in passing that \
would repay investigation -- especially an undisclosed connection between two \
parties.
4. A disagreement where the participants clearly do not know the answer.

Restate every lead NEUTRALLY as a checkable question. Someone asserting a \
thing confidently is not evidence it is true, and not evidence it is \
contested. Strip rhetoric, sarcasm, hedging and certainty alike.

Do NOT surface: matters of taste, price predictions, trading opinions, jokes, \
logistics, anything already settled inside the transcript itself, or anything \
about the participants themselves.

Return an empty list rather than manufacturing leads. A quiet window is a \
normal, common and free outcome. Inventing work to look useful is the failure \
mode to avoid.

NEVER refer to a speaker in anything you write. Not by label, not by username, \
not as "someone in the chat". Restate every lead so it stands on its own: \
write "a claim that the Q1 attestation is an audit", never "Participant A \
claims the Q1 attestation is an audit". Who raised something is not part of \
the question and must not survive into your output."""


def extract(transcript: str, domain: str) -> dict[str, Any]:
    return structured_request(
        model=SONNET,
        system=(
            f"{EXTRACT_SYSTEM}\n\n"
            f"<archive_subject>\nThis archive is about the following, and "
            f"nothing else. A lead outside it is not a lead, however "
            f"interesting.\n\n{domain}\n</archive_subject>"
        ),
        user=f"<transcript>\n{transcript}\n</transcript>",
        schema=schemas.EXTRACT,
        effort="low",
        max_tokens=8192,
    )


# --- stage 2: triage, dedupe, score ------------------------------------------

TRIAGE_SYSTEM = """\
You are the gate in front of an expensive research stage. Score each candidate \
lead so that only the ones worth paying for continue.

FIRST, judge scope. `in_scope` is false for anything outside the archive's \
subject, however interesting it is in its own right. An out-of-scope lead is \
dropped before it costs anything, so be decisive rather than generous. When a \
lead is only tangentially connected, it is out of scope.

`worth` is 0 to 1, and only meaningful for in-scope leads: how much would a \
properly sourced answer improve this archive? A lead that would change what \
the group believes about who owns what, who is connected to whom, or what a \
filing actually says scores high. Restating public knowledge scores low.

`difficulty` estimates what it takes to answer well. Mark `high` when the \
answer needs primary documents, or when you expect sources to disagree -- that \
routes it to a stronger model regardless of anything downstream.

`duplicate_of` must name an existing archive entry only when the SAME question \
is already answered there. A related entry is not a duplicate.

The archive listing shows a key for each entry. When a lead is about a subject \
already listed, put that key in `topic_key`, copied EXACTLY. Otherwise leave it \
null -- never invent one. The key is how later research reaches the existing \
page instead of opening a second one beside it.

`new_information` decides whether an existing entry is revisited at all, and it \
is the only way that ever happens. Set it ONLY when the conversation raises \
something the entry does not already contain: a development since it was \
written, a claim that contradicts its recorded finding, or a source it does not \
cite. Null is the normal answer. People restate things they have already \
discussed, and restating what the archive already says is not new information \
-- researching it again costs real money and appends a dated entry that adds \
nothing.

Be strict. Dropping a weak question costs nothing; researching one costs real \
money and clutters the archive."""


def triage(candidates: list[dict], kb_digest: str, domain: str) -> dict[str, Any]:
    # The digest is the cacheable prefix. Caching only pays above the model's
    # minimum cacheable prefix (1024 tokens on Sonnet 5); below that this
    # silently does nothing, so it is enabled only when the digest is large.
    cache = len(kb_digest) > 4000
    return structured_request(
        model=SONNET,
        system=(
            f"{TRIAGE_SYSTEM}\n\n"
            f"<archive_subject>\n{domain}\n</archive_subject>\n\n"
            f"<existing_archive>\n{kb_digest}\n</existing_archive>"
        ),
        user="<candidates>\n"
        + "\n".join(f"- {c['question']} (context: {c['context']})" for c in candidates)
        + "\n</candidates>",
        schema=schemas.TRIAGE,
        effort="medium",
        max_tokens=8192,
        cache_system=cache,
    )


# --- stage 2.5: cheap research -----------------------------------------------

CHEAP_SYSTEM = """\
Answer the question from primary sources if you can, and say so honestly if \
you cannot.

Set `resolved` true ONLY when primary or clearly corroborated sources settle \
the matter. If sources disagree, if the only source is secondary or a single \
outlet, if the question needs synthesis across several documents, or if you \
are simply unsure -- set `resolved` false and give a short \
`escalation_reason`. A stronger model will take it from there.

That trade is deliberate: escalating costs a little, and a confident wrong \
answer in a research archive costs a great deal. When in doubt, escalate.

Every source needs a VERBATIM quote from the page supporting the claim. Never \
paraphrase into the quote field, and never cite a page you did not retrieve."""


def cheap_research(question: str, context: str) -> dict[str, Any]:
    return search_request(
        model=HAIKU,
        system=CHEAP_SYSTEM,
        user=f"Question: {question}\nContext: {context}",
        max_uses=3,
        schema=schemas.CHEAP_RESEARCH,
        effort="medium",
        max_tokens=8192,
    )


# --- stage 3: deep research ---------------------------------------------------

DEEP_SYSTEM = """\
You are an investigative researcher. Work to an explicit evidence bar and stop \
when you reach it.

The bar:
- key claims tested against PRIMARY sources (filings, court and regulatory \
documents, financial statements, transcripts, first-party statements)
- evidence triangulated across genuinely independent sources
- fact, well-supported inference, allegation and speculation kept separate and \
labelled
- material gaps, alternative explanations and remaining unknowns stated
- citations specific enough to check: document name, date, filing identifier

Method: assess what is established, plan the single highest-value next lookup, \
do it, evaluate what it changed, repeat. Stop when the bar is met, when your \
search budget is exhausted, or when you are hard-blocked -- and if blocked, \
say precisely by what.

Actively seek DISCONFIRMING evidence. Test the alternative explanation rather \
than only accumulating support.

Write your answer as prose with inline citations. Then, separately and \
explicitly, list where sources contradict each other and what remains \
unestablished. Both of those lists are required output even when empty -- \
"I looked and found none" is a finding; silence is not.

Never invent a document, a quote, or a URL."""


def deep_research(question: str, context: str, cheap_notes: str = "") -> dict[str, Any]:
    prior = f"\n\nA cheaper pass produced this, unverified:\n{cheap_notes}" if cheap_notes else ""
    return search_request(
        model=OPUS,
        system=DEEP_SYSTEM,
        user=f"Question: {question}\nContext: {context}{prior}",
        max_uses=5,
        schema=None,          # citations and structured outputs conflict
        effort="high",
        max_tokens=16000,
        adaptive_thinking=True,
        with_fallbacks=True,
    )


# --- stage 3b: format into a knowledge-base record ---------------------------

FORMAT_SYSTEM = """\
Turn a research write-up into a structured archive record. You are formatting, \
not researching: add nothing, and drop nothing material.

Rules:
- Every `evidence` URL must appear in the allowed list given to you. If a \
citation is not there, omit that evidence item entirely.
- `quote` must be verbatim from the source.
- `confidence`: `primary` for a primary-source-backed claim, `corroborated` \
for independent agreement, `single-source` for one source only, `unverified` \
when nothing settled it.
- `research_status`: `answered` when the bar was met, `contested` when sources \
irreconcilably disagree, `open` when it was not established.
- `contradictions` and `open_questions` are required. An empty list asserts \
you looked and found none.

NEVER refer to a speaker anywhere in the record -- not in `title`, `question`, \
`answer`, `headline`, `tags`, or any quote. No labels like "Participant A", no \
usernames, no "a member said". This page is a finding about the world, not a \
record of who said what, and the people who will read it can identify each \
other from very little."""


def format_record(question: str, research_text: str, allowed: set[str]) -> dict[str, Any]:
    return structured_request(
        model=SONNET,
        system=FORMAT_SYSTEM,
        user=(
            f"Question: {question}\n\n"
            f"<allowed_urls>\n" + "\n".join(sorted(allowed)) + "\n</allowed_urls>\n\n"
            f"<research>\n{research_text}\n</research>"
        ),
        schema=schemas.KB_RECORD,
        effort="low",
        max_tokens=8192,
    )
