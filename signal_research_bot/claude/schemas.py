"""JSON Schemas for `output_config.format` at each machine-read stage.

Structured outputs constrain the response to valid JSON matching the schema,
which removes a whole class of parsing failure. Two API constraints apply
everywhere below: every object needs `additionalProperties: false`, and every
property must be listed in `required` (optionality is expressed by allowing
null in the type, not by omitting from required).

One project-specific rule, from .claude/skills/claude-cascade: `contradictions`
and `open_questions` are REQUIRED, not optional. An empty list must be a
deliberate assertion that none were found, never the absence of the field --
otherwise "no contradictions" and "I didn't look" are indistinguishable in the
knowledge base.

Note also: schemas are cached server-side for ~24h and are explicitly carved
out of the enhanced data protections. Nothing derived from chat content may
appear in a property name, enum, const or pattern.
"""

from __future__ import annotations

from typing import Any

CONFIDENCE = ["primary", "corroborated", "single-source", "unverified"]


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# --- stage 1: extract candidate research tasks -------------------------------

EXTRACT = _obj(
    {
        "tasks": {
            "type": "array",
            "items": _obj(
                {
                    "question": {
                        "type": "string",
                        "description": "The research question, restated neutrally. "
                        "Strip rhetoric and assertion: someone stating a thing "
                        "confidently is not evidence it is true or contested.",
                    },
                    "raised_by": {
                        "type": "string",
                        "description": "Speaker label only, e.g. 'Participant A'.",
                    },
                    "context": {
                        "type": "string",
                        "description": "One sentence of surrounding context.",
                    },
                    "kind": {
                        "type": "string",
                        "description": "How the lead arose in conversation. "
                        "'claim' and 'entity' matter as much as 'question' -- "
                        "most real leads are not phrased as questions.",
                        "enum": ["question", "claim", "entity", "disagreement"],
                    },
                },
                ["question", "raised_by", "context", "kind"],
            ),
        }
    },
    ["tasks"],
)


# --- stage 2: triage, dedupe against the KB, and score ------------------------

TRIAGE = _obj(
    {
        "tasks": {
            "type": "array",
            "items": _obj(
                {
                    "question": {"type": "string"},
                    "in_scope": {
                        "type": "boolean",
                        "description": "Is this within the archive's stated "
                        "subject? False drops it before it costs anything.",
                    },
                    "worth": {
                        "type": "number",
                        "description": "0-1. How much would a sourced answer "
                        "improve the archive? Banter, rhetorical questions and "
                        "matters of taste score near 0.",
                    },
                    "difficulty": {"type": "string", "enum": ["low", "medium", "high"]},
                    "duplicate_of": {
                        "type": ["string", "null"],
                        "description": "Title of an existing KB entry this "
                        "duplicates, or null.",
                    },
                    "rationale": {"type": "string"},
                },
                ["question", "in_scope", "worth", "difficulty", "duplicate_of",
                 "rationale"],
            ),
        }
    },
    ["tasks"],
)


# --- stage 2.5: cheap research attempt ---------------------------------------

CHEAP_RESEARCH = _obj(
    {
        "resolved": {
            "type": "boolean",
            "description": "True ONLY if primary sources settle this. If you "
            "are not confident, set false and let a stronger model take it. "
            "A confident wrong answer is far more costly than an escalation.",
        },
        "answer": {"type": "string"},
        "confidence": {"type": "string", "enum": CONFIDENCE},
        "sources": {
            "type": "array",
            "items": _obj(
                {
                    "url": {"type": "string"},
                    "quote": {
                        "type": "string",
                        "description": "Verbatim text from the source that "
                        "supports the claim. Not a paraphrase.",
                    },
                },
                ["url", "quote"],
            ),
        },
        "escalation_reason": {"type": ["string", "null"]},
    },
    ["resolved", "answer", "confidence", "sources", "escalation_reason"],
)


# --- stage 3b: the knowledge-base record -------------------------------------

KB_RECORD = _obj(
    {
        "title": {
            "type": "string",
            "description": "Condensed question. Becomes the wikilink target, "
            "so it must be stable.",
        },
        "question": {"type": "string"},
        "answer": {"type": "string"},
        "confidence": {"type": "string", "enum": CONFIDENCE},
        "research_status": {
            "type": "string",
            "enum": ["open", "researching", "answered", "contested", "dropped"],
        },
        # research_status is workflow state and confidence is evidence
        # strength. Neither says what the answer WAS. In an investigative
        # archive "we checked and it is false" is often the most valuable
        # result, and without this field it is invisible outside the page body.
        "finding": {
            "type": "string",
            "description": "The substantive result. 'refuted' when the claim "
            "as raised is contradicted by the sources -- use it plainly, a "
            "debunking is a real finding. 'mixed' when partly true. "
            "'unestablished' when the sources do not settle it either way.",
            "enum": ["supported", "refuted", "mixed", "unestablished"],
        },
        "headline": {
            "type": "string",
            "description": "One plain sentence stating the result, readable on "
            "its own with no context. Max ~140 characters. This is what people "
            "see in the chat summary, so it must carry the actual answer, not "
            "restate the question.",
        },
        "evidence": {
            "type": "array",
            "items": _obj(
                {
                    "url": {"type": "string"},
                    "quote": {"type": "string"},
                    "confidence": {"type": "string", "enum": CONFIDENCE},
                },
                ["url", "quote", "confidence"],
            ),
        },
        # Required on purpose. See module docstring.
        "contradictions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ways the sources disagree. Empty list means you "
            "looked and found none.",
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What remains unestablished. Empty list means you "
            "looked and found none.",
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    [
        "title", "question", "answer", "confidence", "research_status",
        "finding", "headline", "evidence", "contradictions", "open_questions",
        "tags",
    ],
)


def as_format(schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap a schema for `output_config.format`."""
    return {"type": "json_schema", "schema": schema}
