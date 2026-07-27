"""The only place in this codebase that calls Anthropic.

The point of this module is that the egress firewall is not something a call
site can forget. `send()` runs `egress.guard` on every request before it
leaves, and `egress.check_inbound` on every response before it is returned.
There is no code path that reaches the API without both.

Everything else here is the error handling the API actually requires, which is
easy to get wrong in ways that only show up in production:

* `stop_reason == "refusal"` returns **HTTP 200** with a possibly EMPTY content
  array. Reading `content[0].text` crashes. It is checked first, always.
* `stop_details` is populated only on refusal -- guard before reading it.
* Empty web-search results mean "no results", not an error. Retrying is wrong.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..egress import EgressViolation, Policy, check_inbound, guard

log = logging.getLogger(__name__)

OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"

# Opus 5 can decline under its elevated safety classifiers. Fallbacks are
# opt-in: without this a refused request simply stops. "default" routes by
# refusal category rather than pinning a model, so there is no migration owed
# when a pinned fallback is retired.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Two web-search tool versions, and picking the wrong one is a 400 rather than
# a degraded result.
#
# `web_search_20260209` adds dynamic filtering and is available on Opus 5 /
# 4.8 / 4.7 / 4.6, Fable 5, Sonnet 5 and Sonnet 4.6 -- NOT on Haiku 4.5, which
# is exactly the model the cheap research stage uses. Sending it there rejects
# every task in the window, and because the failure surfaces as a
# BadRequestError rather than a Refusal it was not caught by the per-task
# handler either: the window would end having written nothing, and exit 0.
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_SEARCH_TOOL_BASIC = "web_search_20250305"
DYNAMIC_FILTER_MODELS = frozenset({OPUS, SONNET})

# Same model gate as the dynamic-filtering SEARCH tool, and there is no basic
# fallback in this codebase: a stage that cannot have this simply does not get
# it. Which stage gets it is decided by the caller passing `fetch=`, never
# inferred from the model -- the two happen to coincide today, and a rule that
# is true by coincidence is the kind that breaks silently when one of them
# moves.
WEB_FETCH_TOOL = "web_fetch_20260209"
# Models that accept output_config.effort. Haiku 4.5 does not.
EFFORT_MODELS = frozenset({OPUS, SONNET})


def web_search_tool_for(model: str) -> str:
    """The search tool version this model actually accepts."""
    return WEB_SEARCH_TOOL if model in DYNAMIC_FILTER_MODELS else WEB_SEARCH_TOOL_BASIC


class Refusal(RuntimeError):
    """The model declined. Not retryable with the same input."""

    def __init__(self, category: str | None, explanation: str | None):
        super().__init__(f"model refused (category={category})")
        self.category = category
        self.explanation = explanation


class Transport(Protocol):
    """Minimal surface of anthropic.Anthropic, so tests need no network."""

    def create(self, **kwargs: Any) -> Any: ...


@dataclass
class AnthropicTransport:
    api_key: str | None = None
    _client: Any = None

    def __post_init__(self) -> None:
        import anthropic  # noqa: PLC0415

        self._client = (
            anthropic.Anthropic(api_key=self.api_key)
            if self.api_key
            else anthropic.Anthropic()
        )

    def create(self, **kwargs: Any) -> Any:
        if kwargs.pop("_beta", False):
            return self._client.beta.messages.create(**kwargs)
        return self._client.messages.create(**kwargs)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    fetches: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.searches += other.searches
        self.fetches += other.fetches


@dataclass
class Client:
    policy: Policy
    quarantine_dir: Path
    transport: Transport
    usage: Usage = field(default_factory=Usage)
    # URLs the search tool returned on the most recent call. Read straight
    # after a search stage; this is the ONLY legitimate source for the
    # citation allowlist. See retrieved_urls() for why.
    last_retrieved_urls: set[str] = field(default_factory=set)

    def send(self, **request: Any) -> tuple[str, Usage]:
        """One request. Returns (text, usage). Raises before sending if unsafe."""
        beta = request.pop("_beta", False)

        # Fails closed and quarantines. Nothing reaches the network past here
        # without passing.
        sha = guard(request, self.policy, self.quarantine_dir)

        response = self.transport.create(**request, _beta=beta)

        # Checked FIRST: a refusal is a 200 whose content array may be empty,
        # so any attempt to read the body before this crashes.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise Refusal(
                getattr(details, "category", None),
                getattr(details, "explanation", None),
            )

        usage = _usage_of(response)
        self.usage.add(usage)
        self.last_retrieved_urls = retrieved_urls(response)

        text = _text_of(response)
        # The model can echo back what it was given, and this text is about to
        # be written into a file other people read.
        check_inbound(text, self.policy)
        log.info(
            "claude call complete",
            extra={
                "model": request.get("model"),
                "request_sha": sha,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "searches": usage.searches,
            },
        )
        return text, usage

    def send_json(self, **request: Any) -> tuple[dict[str, Any], Usage]:
        """A structured-output request. Returns the parsed object."""
        text, usage = self.send(**request)
        try:
            return json.loads(text), usage
        except json.JSONDecodeError as exc:
            # Never log the body: it contains transcript-derived content.
            raise ValueError(
                f"structured output was not valid JSON ({len(text)} chars)"
            ) from exc


def _text_of(response: Any) -> str:
    parts = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def retrieved_urls(response: Any) -> set[str]:
    """URLs the SEARCH TOOL actually returned, read from the response blocks.

    This is the ground truth for citation checking, and it must come from
    `web_search_tool_result` blocks rather than from anything the model wrote.

    An audit caught the original version doing the latter: the allowlist was
    built from the `sources` array in the model's own structured output, then
    used to validate the `evidence` array in the model's own structured output.
    A fabricated URL appears in both, so it validated itself. The control read
    as structural and was circular.
    """
    urls: set[str] = set()
    for block in getattr(response, "content", None) or []:
        kind = getattr(block, "type", "")
        content = getattr(block, "content", None)

        if kind == "web_search_tool_result":
            # An error block has a single object here, not a list of results.
            if not isinstance(content, (list, tuple)):
                continue
            for result in content:
                url = getattr(result, "url", None)
                if url:
                    urls.add(url.strip())

        elif kind == "web_fetch_tool_result":
            # A fetch result's content is an OBJECT, not a list -- so the shape
            # check above cannot be reused, and more importantly a *failed*
            # fetch is also an object. Discriminating on shape would harvest the
            # error block's url and launder a page nobody retrieved into the
            # citation allowlist, which is the exact circularity this function
            # exists to prevent. The type tag is what tells them apart.
            if getattr(content, "type", "") != "web_fetch_result":
                continue
            url = getattr(content, "url", None)
            if url:
                urls.add(url.strip())
    return urls


def _usage_of(response: Any) -> Usage:
    raw = getattr(response, "usage", None)
    searches = fetches = 0
    server = getattr(raw, "server_tool_use", None)
    if server is not None:
        # Defaulted rather than assumed present: a response from a stage that
        # carried no fetch tool has no such counter, and the whole point of
        # recording it is that a run which fetched nothing reads as zero rather
        # than as an error.
        searches = getattr(server, "web_search_requests", 0) or 0
        fetches = getattr(server, "web_fetch_requests", 0) or 0
    return Usage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        searches=searches,
        fetches=fetches,
    )


# --- request builders ---------------------------------------------------------
#
# Kept as plain functions returning dicts so a reviewer can see the exact body
# that will be sent, and so tests can assert on it without a network call.


def structured_request(
    *, model: str, system: str, user: str, schema: dict[str, Any],
    effort: str = "low", max_tokens: int = 8192, thinking_disabled: bool = True,
    cache_system: bool = False,
) -> dict[str, Any]:
    system_block: Any = system
    if cache_system:
        # Only worth it above the model's minimum cacheable prefix (1024 tokens
        # on Sonnet 5); below that this silently does nothing.
        system_block = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
        ]
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_block,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    }
    if thinking_disabled:
        request["thinking"] = {"type": "disabled"}
    return request


def search_request(
    *, model: str, system: str, user: str, max_uses: int,
    schema: dict[str, Any] | None = None, effort: str = "high",
    max_tokens: int = 16000, adaptive_thinking: bool = False,
    with_fallbacks: bool = False, fetch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A web-search request, optionally able to read specific pages too.

    `schema` is deliberately optional and unset for the deep-research stage:
    citations and structured outputs have a documented conflict, so that stage
    returns free text and a separate cheap call turns it into a record.

    `fetch` is an explicit parameter and not something this function decides
    from the model. `web_fetch_20260209` has the same model gate as the
    dynamic-filtering search tool, so inferring it from `DYNAMIC_FILTER_MODELS`
    would work today and would be wrong in principle: whether a stage is allowed
    to read a link chosen by chat participants is a policy decision, not a
    capability lookup, and the two would part company the moment either moved.
    Passed as a whole tool block so the caller owns `max_uses`, the content cap
    and the blocked-domain list.
    """
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [
            {
                "type": web_search_tool_for(model),
                "name": "web_search",
                "max_uses": max_uses,
            }
        ],
        "output_config": {},
    }
    # Appended, so search stays tools[0]. Several tests index it positionally,
    # and a silent reorder would move what they assert on rather than fail.
    if fetch is not None:
        request["tools"].append(fetch)
    # `effort` is not universal either -- sending it to a model that does not
    # take it is a 400, not a silently ignored field.
    if model in EFFORT_MODELS:
        request["output_config"]["effort"] = effort
    if schema is not None:
        request["output_config"]["format"] = {"type": "json_schema", "schema": schema}
    if not request["output_config"]:
        del request["output_config"]
    if adaptive_thinking:
        request["thinking"] = {"type": "adaptive"}
    if with_fallbacks:
        request["_beta"] = True
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"
    return request
