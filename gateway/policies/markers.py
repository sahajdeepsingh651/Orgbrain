"""G2 — Positional marker authorization.

The one genuine vulnerability this gateway must close: a marker found
in a `tool_result` block (a file the AI just read, a tool's reply), in
a prior assistant turn, or in any position other than the last genuine
human turn, must NEVER be honoured. A repo file containing
`ESDS_SUBMIT {...}` or `ESDS_APPROVE <id>` read into a tool_result would
otherwise publish attacker-controlled content into the org brain with no
human involved.

The rule is exactly the rule `read.is_new_human_turn` already encodes for
"genuine human turn, look through one trailing harness role:'system'
message, never a tool-loop hop". This module reuses that look-through to
search the text of ONLY that turn — so the predicate and the "is this a
fresh turn" check cannot drift apart, the way a reimplementation would.

Markers covered (all four go through ONE predicate):
    ESDS_SEARCH         — retrieve documents behind identity-gated search
    ESDS_SUBMIT         — request the LLM draft a knowledge record
    ESDS_APPROVE <id>   — the only marker that commits anything to the bus;
                          the highest-value injection target in the system
    ESDS_REJECT <id>     — clear a pending draft

All matching is anchored at the line level so a marker embedded mid-line
or inside a fenced block inside a tool_result cannot authorise either
(see the test matrix in gateway/tests/test_g2_markers.py).

This module is deliberately stateless and side-effect free; it returns the
matched marker or None. The flow that acts on the marker (READ, WRITE,
approval machinery) lives in the policy that owns that flow.
"""
from __future__ import annotations

from ..protocol.normalized import NormalizedMessage, NormalizedRequest
from .read import is_new_human_turn


def last_human_turn_text(nr: NormalizedRequest) -> str:
    """The concatenated text of only the last genuine human turn, applying
    the same look-through rule `is_new_human_turn` uses — one trailing
    harness role:'system' message is allowed. Only type=='text' blocks
    contribute (tool_result content does NOT — that is exactly what an
    attacker would hide a marker inside)."""
    messages = nr.messages
    if not messages:
        return ""
    last = messages[-1]
    if last.role == "system" and len(messages) >= 2:
        last = messages[-2]
    if last.role != "user":
        return ""
    # Walk text blocks of THIS message only — never tool_result content.
    return "\n".join(
        b.get("text", "") for b in last.content if b.get("type") == "text"
    )


def _line_anchored_match(line: str, marker: str) -> str | None:
    """A marker is honoured only on a line that starts with it (optionally
    after leading whitespace). Mid-line or text-block-internal appearances
    are NOT honoured — a marker quoted inside prose should not trigger.

    A marker must be followed by end-of-line, whitespace, or a colon — so
    `ESDS_SEARCH alone` honours, `ESDS_SEARCH: please` honours, but
    `ESDS_SEARCH_TOOL` (a different identifier) does not (the char after
    `marker` is an underscore-continuation, not a delimiter)."""
    stripped = line.lstrip()
    if stripped == marker:
        return stripped
    if stripped.startswith(marker):
        nxt = stripped[len(marker)]
        if nxt in (" ", "\t", ":"):
            return stripped
    return None


def find_marker(nr: NormalizedRequest, marker: str) -> str | None:
    """Return the matched marker line if `marker` appears in the last
    genuine human turn at line start, else None.

    Returns the full stripped line (so ESDS_APPROVE callers can parse the
    `<id>` argument off it). Honours the position invariant: never from
    tool_result, files, prior turns, or the trailing harness system message
    content."""
    last_text = last_human_turn_text(nr)
    if not last_text or not is_new_human_turn(nr):
        return None
    for line in last_text.splitlines():
        m = _line_anchored_match(line, marker)
        if m is not None:
            return m
    return None


def find_marker_arg(nr: NormalizedRequest, marker: str) -> str | None:
    """For ESDS_APPROVE <id> / ESDS_REJECT <id>: return the token after the
    marker on the matched line, or None if no marker or no argument."""
    line = find_marker(nr, marker)
    if line is None:
        return None
    parts = line.split()
    # ["ESDS_APPROVE", "<id>"] or ["ESDS_APPROVE", "<id>", ...flags]
    if len(parts) < 2:
        return None
    return parts[1]


def marker_remainder(nr: NormalizedRequest, marker: str) -> str:
    """Everything after the marker on its line, stripped of a leading
    colon: `ESDS_SEARCH: push notifications` -> `push notifications`.
    Empty string when the marker stands alone."""
    line = find_marker(nr, marker)
    if line is None:
        return ""
    rest = line[len(marker):].lstrip()
    if rest.startswith(":"):
        rest = rest[1:].lstrip()
    return rest.strip()


def strip_marker(nr: NormalizedRequest, marker: str) -> NormalizedRequest:
    """Remove the marker line from what the LLM sees.

    The marker is protocol between the human and the interceptor, not
    content for the model — leaving it in makes the model try to interpret
    a token it was never told about, and (worse) puts the literal string
    into the conversation history, where on a LATER turn it would sit in a
    prior user turn. find_marker already refuses to honour it there, but
    not leaving litter is cheaper than relying on that.

    Only the last genuine human turn is touched, and only whole lines that
    `_line_anchored_match` accepts — the same predicate that authorised the
    marker in the first place, so the two cannot drift.
    """
    messages = nr.messages
    if not messages:
        return nr
    idx = len(messages) - 1
    if messages[idx].role == "system" and len(messages) >= 2:
        idx = len(messages) - 2
    target = messages[idx]
    if target.role != "user":
        return nr

    new_content, changed = [], False
    for block in target.content:
        if block.get("type") != "text" or "text" not in block:
            new_content.append(block)
            continue
        kept = []
        for ln in block["text"].splitlines():
            if _line_anchored_match(ln, marker) is not None:
                # The line starts with the marker. We should strip the marker
                # but preserve any query text that follows it.
                rest = ln[ln.find(marker) + len(marker):].lstrip()
                if rest.startswith(":"):
                    rest = rest[1:].lstrip()
                if rest:
                    kept.append(rest)
            else:
                kept.append(ln)
                
        new_text = "\n".join(kept).strip()
        if new_text != block["text"]:
            changed = True
        # Drop a block that held nothing but the marker, rather than
        # sending an empty text block (the API rejects those).
        if new_text:
            new_content.append({**block, "text": new_text})
    if not changed:
        return nr

    # A user turn must not end up with zero blocks.
    if not new_content:
        new_content = [{"type": "text", "text": "(context request)"}]

    updated = list(messages)
    updated[idx] = NormalizedMessage(role=target.role, content=new_content)
    return nr.clone_with_messages(updated)


# Resolution table for the response side: NEVER match these on the
# response. find_marker is request-side only — the marker the AI DRAFTS
# in its reply (G6's "EXTRACTED"/fenced JSON block) is detected by WRITE's
# own response-text scan, never here.
MARKERS = ("ESDS_SEARCH", "ESDS_SUBMIT", "ESDS_APPROVE", "ESDS_REJECT")
