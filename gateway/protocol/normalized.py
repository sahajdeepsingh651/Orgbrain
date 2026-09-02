"""Provider-neutral internal representation.

Policies (gateway/policies/*) operate ONLY on these types — no
Anthropic/OpenAI-specific JSON logic may appear there. Protocol adapters
(gateway/protocol/*_adapter.py) are the only code allowed to know what a
request or response actually looks like on the wire.

Tool calls and tool results are represented as content-block variants
(``type: "tool_use"`` / ``type: "tool_result"``), not as separate parallel
fields — a tool interaction IS content, not a different kind of thing.

The canonical block-type vocabulary intentionally reuses Anthropic's names
(text / tool_use / tool_result / image / document) rather than inventing
neutral synonyms. Anthropic already models tool interaction as content,
which is the property we want canonically; an adapter for a protocol that
does NOT model it that way (e.g. OpenAI's `tool_calls` field plus
`role:"tool"` messages) is the one that translates onto this vocabulary,
not the reverse. This keeps AnthropicAdapter close to an identity
transform and pushes real translation work onto the adapter that actually
needs to do it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NormalizedMessage:
    role: str  # "user" | "assistant" | "system"
    # Each block is a dict with at least {"type": ...}. Block shape follows
    # the vocabulary described in the module docstring above.
    content: list[dict]


@dataclass
class NormalizedRequest:
    model: str | None
    system_context: str | list[dict] | None
    messages: list[NormalizedMessage]
    stream: bool
    metadata: dict = field(default_factory=dict)
    # Everything the adapter didn't model explicitly (max_tokens, tools,
    # tool_choice, thinking, output_config, beta-specific fields, ...),
    # preserved verbatim so from_normalized() round-trips losslessly. This
    # is the field that keeps the adapter from silently dropping legitimate
    # request fields it doesn't happen to know about.
    extra: dict = field(default_factory=dict)

    def clone_with_messages(self, messages: list[NormalizedMessage]) -> "NormalizedRequest":
        """Functional update — policies never mutate a NormalizedRequest in
        place, matching the copy-don't-mutate style the rest of this
        codebase already uses."""
        return NormalizedRequest(
            model=self.model,
            system_context=self.system_context,
            messages=messages,
            stream=self.stream,
            metadata=dict(self.metadata),
            extra=dict(self.extra),
        )


@dataclass
class NormalizedResponse:
    model: str | None
    text: str  # concatenated assistant text
    stop_reason: str | None
    usage: dict  # input_tokens, cache_read_input_tokens, cache_creation_input_tokens, output_tokens
    status_code: int = 200
    is_error: bool = False
