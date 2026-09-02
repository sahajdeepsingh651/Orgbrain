"""READ policy: detect a genuine human turn -> query the Context DB ->
inject the returned document into the request -> forward.

Operates ONLY on NormalizedRequest. No Anthropic/OpenAI-specific JSON
appears here — see gateway/protocol/*_adapter.py for wire-format details.

The Context DB query itself is not yet implemented (docs/ARCHITECTURE.md §2 —
semantic retrieval is out of scope for docs/TEST-PLAN.md's T0-T4). `apply()`
here is driven by the DP_INJECT/DP_INJECT_TEXT test scaffolding instead,
standing in for "the retrieval step decided this document is relevant."
"""

from __future__ import annotations

import os

from ..protocol.normalized import NormalizedMessage, NormalizedRequest


def is_new_human_turn(nr: NormalizedRequest) -> bool:
    """True if the last message is a genuine human turn, not a tool-loop
    hop. A user turn whose content is entirely tool_result blocks is the
    agent continuing a tool loop, not a person asking something new —
    injecting on every hop would repeat the same context many times per
    turn.

    Real Claude Code traffic routinely ends the request with the
    harness's OWN trailing role="system" message (agent list, skills, hook
    output) appended after the actual human turn — confirmed against the
    real API, not assumed. AnthropicAdapter._serialize_messages folds any
    such message into whichever turn precedes it on the wire, so for the
    purpose of "is this a fresh turn to inject into", look through one
    trailing system message to what it will actually be attached to.
    Without this, injection silently never fires on real traffic, despite
    passing on every synthetic fixture that doesn't model this shape.
    """
    messages = nr.messages
    if not messages:
        return False
    last = messages[-1]
    if last.role == "system" and len(messages) >= 2:
        last = messages[-2]
    if last.role != "user":
        return False
    return any(b.get("type") == "text" for b in last.content)


def add_context(nr: NormalizedRequest, document_text: str) -> NormalizedRequest:
    """Append `document_text` as an authoritative, injected context turn.

    Uses role="system" in the NORMALIZED representation as the abstract
    "injected, higher-trust context" marker. Whether this survives as a
    literal system-role message on the wire, or gets rewritten into
    something else entirely, is the ADAPTER's decision at serialization
    time (see AnthropicAdapter._serialize_messages) — this function knows
    nothing about model gating or wire formats, by design. That's the
    point: "append to the last user message" is not the universal
    mechanism; "modify the normalized conversation" is, and the adapter
    picks the valid wire-format location.
    """
    injected = NormalizedMessage(role="system", content=[{"type": "text", "text": document_text}])
    return nr.clone_with_messages(list(nr.messages) + [injected])


def apply(nr: NormalizedRequest, *, inject: bool, text: str) -> NormalizedRequest:
    if inject and is_new_human_turn(nr):
        return add_context(nr, text)
    return nr


# --------------------------------------------------------------------------
# G4 — retrieval behind ESDS_SEARCH
#
# `distance` from /v1/search is COSINE distance: lower is more similar. The
# floors below are ceilings on distance, not thresholds on similarity.
#
# The floor matters far more for AWARENESS than for SEARCH. Under
# ESDS_SEARCH the human explicitly asked, sees the results, and can retype;
# a miss is recoverable. Awareness fires unprompted on every turn, so a
# loose floor there produces a permanent distracting banner — which is the
# "wrong context is worse than no context" failure this design exists to
# avoid. Hence two different defaults.
# --------------------------------------------------------------------------
SEARCH_MAX_DISTANCE = float(os.environ.get("DP_SEARCH_MAX_DISTANCE", "1.0"))
AWARENESS_MAX_DISTANCE = float(os.environ.get("DP_AWARENESS_MAX_DISTANCE", "0.62"))


def _short(record_id: str | None) -> str:
    return (record_id or "")[:8]


def _origin(hit: dict) -> str:
    dept, team = hit.get("department") or "?", hit.get("team")
    who = f"{dept}/{team}" if team else dept
    when = (hit.get("created_at") or "")[:10]
    return f"{who}{', ' + when if when else ''}"


def filter_hits(hits: list[dict], max_distance: float) -> list[dict]:
    """Apply the relevance floor. A hit with no `distance` (a pure keyword
    match — /v1/search unions ANN with an ILIKE pass) is kept: it matched
    the query text literally, which is its own evidence of relevance."""
    out = []
    for h in hits:
        d = h.get("distance")
        if d is None or d <= max_distance:
            out.append(h)
    return out


def render_documents(hits: list[dict]) -> str:
    """Full retrieved context for injection after an explicit ESDS_SEARCH.

    Rendered as plain text on purpose: add_context puts this in a
    role="system" normalized message, and AnthropicAdapter's fallback path
    keeps only type=="text" blocks of such a message (non-text blocks are
    dropped silently). Text is the only shape that survives both paths.

    Provenance is stated inline so the model can attribute what it uses,
    and so a human reading the transcript can see this came from a
    colleague's session rather than from the model's own knowledge.
    """
    if not hits:
        return ""
    lines = [
        "The ESDS Gateway proxy has automatically retrieved the following relevant background context for you from the Context Bus. "
        f"(No tool call was required on your part to fetch these {len(hits)} reference record(s)). "
        "Please use this information to help answer the user's query.\n"
        "IMPORTANT: You MUST start your response by explicitly stating 'Retrieved from passport <id>:' so the user knows the source of the information. Cite the IDs of all passports you use."
    ]
    for h in hits:
        lines.append("")
        lines.append(f"[passport {_short(h.get('record_id'))}] {h.get('title', '(untitled)')}")
        if h.get("summary"):
            lines.append(f"  {h['summary']}")
        meta = [f"outcome={h.get('outcome')}" if h.get("outcome") else "",
                f"status={h.get('status')}" if h.get("status") else ""]
        lines.append(f"  — {_origin(h)}" + ("".join(f", {m}" for m in meta if m)))
    return "\n".join(lines)


def render_awareness(hits: list[dict]) -> str:
    """G5 — titles and a count ONLY. Never a document body.

    This is the whole point of separating awareness from retrieval: the
    human should not have to already know a colleague's session exists in
    order to find it, but injecting the bodies unprompted would be the
    blind injection this design rejects. ~20 tokens, no content, and the
    human stays the one who decides to pull it.
    """
    if not hits:
        return ""
    titles = "; ".join(
        f"{h.get('title', '(untitled)')} [{_short(h.get('record_id'))}]" for h in hits
    )
    n = len(hits)
    return (
        f"ESDS Orgbrain: {n} related session{'s' if n != 1 else ''} "
        f"may be relevant — {titles}. "
        "Tell the user they can type ESDS_SEARCH to retrieve the full records. "
        "Do not assume their contents."
    )
