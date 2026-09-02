"""Anthropic Messages API adapter.

Owns every Anthropic-wire-format detail: content-block shapes, the
mid-conversation role:"system" model-gating fallback (docs/ARCHITECTURE.md
§2.2), and SSE/JSON response parsing. Policies never see any of this — they
only see NormalizedRequest / NormalizedResponse.
"""

from __future__ import annotations

import json

from .normalized import NormalizedMessage, NormalizedRequest, NormalizedResponse

# Models known to accept a mid-conversation {"role": "system", ...} message.
# Source: docs/ARCHITECTURE.md §2.2, cross-checked against the Claude API
# reference's "Mid-conversation System Messages" section — both list
# exactly these four and explicitly exclude Sonnet 5. Sending the role to
# an unlisted model returns `400 role 'system' is not supported on this
# model`. Matched by prefix so dated/suffixed variants of a supported model
# still match. Keep this list in sync with both sources if either changes.
_SUPPORTS_MIDCONV_SYSTEM_ROLE = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-fable-5",
    "claude-mythos-5",
)


def _model_supports_system_role(model: str | None) -> bool:
    if not model:
        return False
    return model.startswith(_SUPPORTS_MIDCONV_SYSTEM_ROLE)


def _content_to_blocks(content) -> list[dict]:
    """Anthropic's `content` is either a bare string or a list of content
    blocks; normalize to a list either way. Anthropic's block shapes are
    the canonical normalized vocabulary (see normalized.py's module
    docstring), so blocks pass through unchanged — this is deliberately
    close to an identity transform.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def _extract_session_identity(body: dict) -> dict | None:
    """Best-effort parse of the JSON-string `user_id` Claude Code sends in
    metadata, into the gateway-internal `session_id` + `account_uuid`.

    Claude Code already sends both — no fingerprinting needed:
    `metadata.user_id = '{"device_id":"d87…","account_uuid":"42bfe041-…",
    "session_id":"9964cc38-…"}'` — a JSON STRING inside metadata.user_id
    (not a nested object). Confirmed in all three fixtures/*.json: two of
    three share a session_id (stable within a session), the third differs.

    Never raise: a harness that doesn't send metadata (most test bodies)
    must still work; fall back to None for every missing piece.
    """
    try:
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            return None
        user_id = metadata.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip().startswith("{"):
            return None
        parsed = json.loads(user_id)
        if not isinstance(parsed, dict):
            return None
        out: dict = {}
        if isinstance(parsed.get("session_id"), str):
            out["session_id"] = parsed["session_id"]
        if isinstance(parsed.get("account_uuid"), str):
            out["account_uuid"] = parsed["account_uuid"]
        return out or None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _normalize_messages_for_compare(messages_raw) -> list:
    """GT — re-normalize wire messages into the same shape to_normalized
    produces, so mutation detection compares apples to apples."""
    out = []
    for m in messages_raw or []:
        out.append({
            "role": m.get("role", "user"),
            "content": _content_to_blocks(m.get("content")),
        })
    return out


def _messages_were_mutated(nr: "NormalizedRequest", orig_messages) -> bool:
    """GT — has any policy altered nr.messages relative to the original
    wire body? Re-normalize the wire body and compare with nr.messages.
    A policy that ADDED a {role:system,...} message (read.add_context)
    or rewrote text blocks (scan) makes them differ; a policy that only
    added metadata (G1, G6's pending_id) does NOT alter nr.messages."""
    if orig_messages is None:
        return bool(nr.messages)
    base = _normalize_messages_for_compare(orig_messages)
    cur = [(m.role, m.content) for m in nr.messages]
    base_pairs = [(b["role"], b["content"]) for b in base]
    return cur != base_pairs


class AnthropicAdapter:
    name = "anthropic"

    # ---- request direction ----

    def to_normalized(self, body: dict) -> NormalizedRequest:
        extra = {k: v for k, v in body.items() if k not in {"model", "system", "messages", "stream"}}
        messages = [
            NormalizedMessage(role=m.get("role", "user"), content=_content_to_blocks(m.get("content")))
            for m in body.get("messages") or []
        ]
        metadata = {"protocol": self.name}
        session = _extract_session_identity(body)
        if session is not None:
            metadata.update(session)
        # GT — passthrough fidelity. Stash the wire presence of `stream` and
        # `system` so from_normalized can preserve them when nothing mutated
        # the request. Pre-redaction, scan() returns the SAME nr object on
        # the empty-vault path (no clone), so these flags survive to the
        # serialize step. Pre-redaction of a matching request, scan() builds
        # a new NormalizedRequest via clone_with_messages-equivalent fields
        # AND copies extra, so the flags survive there too.
        extra["__anth_stream_present"] = "stream" in body
        extra["__anth_system_present"] = "system" in body
        # Stash the original wire messages so a scan that matched NOTHING
        # can emit them byte-for-byte unchanged — the bare-string `content`
        # of a fresh user turn survives. This is a deliberate name double-
        # underscore-prefixed so policies cannot accidentally pick it up;
        # from_normalized strips it.
        extra["__anth_orig_messages"] = body.get("messages")
        return NormalizedRequest(
            model=body.get("model"),
            system_context=body.get("system"),
            messages=messages,
            stream=bool(body.get("stream")),
            metadata=metadata,
            extra=extra,
        )

    def from_normalized(self, nr: NormalizedRequest) -> dict:
        raw = {k: v for k, v in nr.extra.items() if not k.startswith("__anth_")}
        raw["model"] = nr.model
        stream_present = nr.extra.get("__anth_stream_present", False)
        system_present = nr.extra.get("__anth_system_present", False)
        # Decide messages: if no policy mutated nr.messages, emit the
        # original wire list byte-for-byte (bare strings stay bare strings,
        # block-order preserved). Mutated iff nr.messages is no longer the
        # same list identity as the one we built OR extra's stashed original
        # has been replaced. The cleanest check: scan() returns a NEW
        # NormalizedRequest on a match (clone), the SAME object on no match.
        # We compare the normalized messages against the stashed original:
        # if they are content-equal (no policy rewrote a block), emit the
        # original. Else serialize the new one.
        orig_messages = nr.extra.get("__anth_orig_messages")
        emitted_messages = self._serialize_messages(nr) if _messages_were_mutated(nr, orig_messages) else orig_messages
        if emitted_messages is None:
            emitted_messages = self._serialize_messages(nr)
        raw["messages"] = emitted_messages
        # Preserve original `system` presence: a body that sent
        # `"system": null` must round-trip as `"system": null`, not be dropped.
        if system_present:
            raw["system"] = nr.system_context
        # Preserve original `stream` presence: a body with no `stream` key
        # must round-trip with no `stream` key — emitting an explicit
        # `false` adds tokens to the cache prefix and breaks T4.
        if stream_present:
            raw["stream"] = nr.stream
        return raw

    def _serialize_messages(self, nr: NormalizedRequest) -> list[dict]:
        """Anthropic-specific wire-format choice for injected context.

        A NormalizedMessage with role="system" is the policy layer's
        abstract "authoritative, injected context" marker — it does not
        promise a literal role:"system" message on the wire. Whether it
        survives as one depends on whether nr.model supports Anthropic's
        mid-conversation system role; when it doesn't, fold the content
        into the preceding user turn's content instead (the documented
        <system-reminder> fallback — same cache-prefix cost, lower trust,
        per docs/ARCHITECTURE.md §2.2). The policy layer that produced this
        message never had to know any of this.
        """
        supports_system = _model_supports_system_role(nr.model)
        out: list[dict] = []
        for m in nr.messages:
            if m.role == "system" and not supports_system:
                text = "\n".join(b.get("text", "") for b in m.content if b.get("type") == "text")
                reminder = {"type": "text", "text": f"\n\n**Automated Instruction:**\n{text}"}
                if out and out[-1]["role"] == "user":
                    out[-1] = {"role": "user", "content": list(out[-1]["content"]) + [reminder]}
                else:
                    # No preceding user turn to fold into — shouldn't happen
                    # given policies/read.py only injects after a genuine
                    # human turn, but stay correct if it ever does.
                    out.append({"role": "user", "content": [reminder]})
                continue
            out.append({"role": m.role, "content": list(m.content)})
        return out

    # ---- response direction ----

    def parse_response_json(self, status_code: int, body: dict) -> NormalizedResponse:
        """Non-streaming response."""
        text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        return NormalizedResponse(
            model=body.get("model"),
            text=text,
            stop_reason=body.get("stop_reason"),
            usage=body.get("usage") or {},
            status_code=status_code,
            is_error=status_code >= 400,
        )

    def parse_response_sse(self, status_code: int, sse_text: str) -> NormalizedResponse:
        """Streaming response — parse the accumulated SSE text after the
        fact. This is for post-hoc analysis (usage logging, future WRITE
        extraction) only — it is NOT the mechanism CHECK's real-time token
        restoration will use. Restoring redacted tokens as bytes stream
        needs an incremental, boundary-aware buffer hooked directly into
        the relay loop (a token can split across SSE chunks — see
        docs/ARCHITECTURE.md §5's "streaming gotcha"), which is a distinct,
        not-yet-built code path.
        """
        usage: dict = {}
        text_parts: list[str] = []
        model = None
        stop_reason = None
        for line in sse_text.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            t = data.get("type")
            if t == "message_start":
                msg = data.get("message") or {}
                model = msg.get("model", model)
                u = msg.get("usage")
                if u:
                    usage.update(u)
            elif t == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))
            elif t == "message_delta":
                u = data.get("usage")
                if u:
                    usage.update(u)
                sr = (data.get("delta") or {}).get("stop_reason")
                if sr:
                    stop_reason = sr
        return NormalizedResponse(
            model=model,
            text="".join(text_parts),
            stop_reason=stop_reason,
            usage=usage,
            status_code=status_code,
            is_error=status_code >= 400,
        )
