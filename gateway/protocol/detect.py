"""Protocol detection — route/path first, then schema markers, then request
structure, then model/provider string, in that order (cheapest and most
reliable signal first). This detects the WIRE PROTOCOL, never the harness —
Claude Code, Cursor, and a bare SDK script can all speak the same protocol,
and the gateway must not care which one is asking.

Only one adapter exists today (Anthropic). The other tiers are wired in as
explicit no-op stubs so adding a second adapter later is an insertion, not
a restructuring. This is a small ordered function, deliberately not a
plugin registry — that would be premature for two adapters.
"""

from __future__ import annotations

from fastapi import Request

from .anthropic_adapter import AnthropicAdapter

_ANTHROPIC_ADAPTER = AnthropicAdapter()


def detect(request: Request, body: dict | None):
    """Return an adapter instance for this request, or None if unrecognized.

    None means: do not normalize, do not mutate — the caller must fail open
    and forward the raw bytes untouched. This extends docs/ARCHITECTURE.md
    §2.6's fail-open principle (any Context Bus failure -> forward
    unmodified) to a new failure mode: an unrecognized wire protocol.
    """
    # Tier 1 — route/path. Cheapest, most reliable signal; doesn't need the
    # body at all.
    path = request.url.path
    if path.startswith("/v1/messages"):
        return _ANTHROPIC_ADAPTER

    # Tier 2 — explicit protocol markers / schema (e.g. an `anthropic-version`
    # header, or OpenAI's `/v1/chat/completions` request shape). Not needed
    # while only one adapter exists.

    # Tier 3 — request structure heuristics (e.g. `input`/`instructions`
    # fields for OpenAI's Responses API vs `system`/`messages` for
    # Anthropic). Not needed yet.

    # Tier 4 — model/provider string, last resort. Weakest signal (a
    # routing proxy could call any model over any wire shape), but harmless
    # as a final fallback.
    if body and isinstance(body.get("model"), str) and body["model"].startswith("claude-"):
        return _ANTHROPIC_ADAPTER

    return None
