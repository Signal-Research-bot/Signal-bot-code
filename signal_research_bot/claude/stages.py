"""The five cascade stages, as prompts plus request construction.

Model and parameter choices are documented in
.claude/skills/claude-cascade/SKILL.md; the reasoning is not repeated here.
The prompts themselves encode the operator's existing research standard, which
lives in .claude/skills/investigate-loop/SKILL.md: primary sources first,
triangulation, and fact kept separate from inference kept separate from
speculation.

Every prompt below is written on the assumption that the transcript contains
NO identities -- only "Participant A" style labels. Asking the model to avoid
naming people would be a control that runs after the fact and cannot be
verified; the egress firewall is the control, and these prompts simply have no
identities to leak.
"""

from __future__ import annotations

from typing import Any

from . import schemas
from .client import HAIKU, OPUS, SONNET, search_request, structured_request

# --- stage 1: extract ---------------------------------------------------------

EXTRACT_SYSTEM = """\
You read a pseudonymised group chat transcript and pull out questions worth \
researching. Participants are labelled "Participant A" and so on; that is all \
you know about them and all you need.

Extract a task only when answering it with sources would tell the group \
something they do not already know. Restate every question NEUTRALLY: someone \
asserting a thing confidently is not evidence the thing is true, and not \
evidence it is contested. Strip rhetoric, sarcasm and hedging.

Do not extract: matters of taste, jokes, plans to meet, questions already \
answered in the transcript itself, or questions about the participants.

Return an empty list rather than manufacturing tasks. A quiet window is a \
normal outcome and costs nothing."""


def extract(transcript: str) -> dict[str, Any]:
    return structured_request(
        model=SONNET,
        system=EXTRACT_SYSTEM,
        user=f"<transcript>\n{transcript}\n</transcript>",
        schema=schemas.EXTRACT,
        effort="low",
        max_tokens=8192,
    )


# --- stage 2: triage, dedupe, score ------------------------------------------

TRIAGE_SYSTEM = """\
You are the gate in front of an expensive research stage. Score each candidate \
question so that only the ones worth paying for continue.

`worth` is 0 to 1: how much would a properly sourced answer improve a shared \
research archive? Banter, taste, and anything already settled score near 0. A \
question whose answer would change what the group believes or does scores high.

`difficulty` estimates what it takes to answer well. Mark `high` when the \
answer needs primary documents, or when you expect sources to disagree -- that \
routes it to a stronger model regardless of anything downstream.

`duplicate_of` must name an existing archive entry only when the SAME question \
is already answered there. A related entry is not a duplicate.

Be strict. Dropping a weak question costs nothing; researching one costs real \
money and clutters the archive."""


def triage(candidates: list[dict], kb_digest: str) -> dict[str, Any]:
    # The digest is the cacheable prefix. Caching only pays above the model's
    # minimum cacheable prefix (1024 tokens on Sonnet 5); below that this
    # silently does nothing, so it is enabled only when the digest is large.
    cache = len(kb_digest) > 4000
    return structured_request(
        model=SONNET,
        system=f"{TRIAGE_SYSTEM}\n\n<existing_archive>\n{kb_digest}\n</existing_archive>",
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
you looked and found none."""


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
