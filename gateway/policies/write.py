"""WRITE policy — draft extraction, validation, and sensitivity flags.

G6 replaced the previous test-grade mechanism entirely. What changed and why:

  * The old trigger injected `EXTRACTED_DECISION:` unconditionally and
    parsed one line back out with a `startswith` scan. It produced a
    free-text sentence, which cannot populate /v1/ingest's ~10 required
    fields, and it fired on every request rather than on a human's explicit
    ask. Now the trigger is gated on ESDS_SUBMIT in the last genuine human
    turn (policies/markers.py) and asks for a fenced JSON block.
  * The old sink wrote four keys to /tmp and called it "pending_review".
    Now a draft is validated against the real ingest contract, DLP-scanned,
    and parked in gateway/pending.py until a human approves it.

This module stays PURE — no I/O, no bus calls, no disk. It builds the
instruction, finds the draft, checks it, and derives flags. The flow that
acts on the result lives in gateway/flows.py.

Fields the model is NOT allowed to decide, and why:
    session_id      identity, from the adapter (G1). A model-chosen session
                    id would let a draft be filed against someone else's work.
    captured_by     ditto — and since store S5 the bus derives authorship
    hint.department  from the bearer token and ignores these fields anyway.
    visibility      an access-control decision. Defaults to `team` and is
                    the human's to widen, at approval time.
"""
from __future__ import annotations

import json
import re

from ..protocol.normalized import NormalizedRequest, NormalizedResponse
from . import read as read_policy

# Mirrors POST /v1/ingest (store/docs/orgbrain-api-reference.md).
VISIBILITY_VALUES = ("private", "team", "department", "org")
STATUS_VALUES = ("in_progress", "completed", "blocked", "handed_off", "abandoned")
OUTCOME_VALUES = ("decision_made", "insight_found", "issue_resolved",
                  "blocker_hit", "question_open", "in_progress")

DEFAULT_VISIBILITY = "team"
DEFAULT_STATUS = "completed"

# A fenced json block, non-greedy. The model is told
# to emit exactly one; if it emits several we take the LAST, on the theory
# that a model correcting itself puts the good one last.
_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def extraction_instruction(pending_id: str) -> str:
    """The instruction injected when a human types ESDS_SUBMIT.

    The pending id is minted BEFORE the model replies and embedded here so
    the model can print it. That is what keeps the write to two turns: a
    proxy cannot push, so if the gateway waited to reveal the id until the
    next request, the human would have to take an otherwise-pointless turn
    in between just to be told what to type.
    """
    return (
        "Please provide a JSON summary of our conversation so far. "
        "End your reply with exactly one fenced JSON block (```json ... ```) "
        "using this exact shape:\n"
        "{\n"
        '  "content": "<2-4 sentences: what was decided or learned, and why>",\n'
        '  "knowledge": {\n'
        '    "title": "<short label>",\n'
        '    "summary": "<1-3 sentence distillation a stranger could act on>",\n'
        f'    "outcome": "<one of: {", ".join(OUTCOME_VALUES)}>",\n'
        '    "key_points": ["..."],\n'
        '    "next_steps": ["..."]\n'
        "  }\n"
        "}\n"
        "Do not include session ids, user ids, department, or visibility. Base it only on what actually happened in this conversation; "
        "do not invent decisions that were not made.\n"
        f"Then on the next line, please output exactly: To save this, type ESDS_APPROVE {pending_id}"
    )


def inject_extraction_trigger(nr: NormalizedRequest, pending_id: str) -> NormalizedRequest:
    """Same injection primitive as READ, so the model-gated wire placement
    (literal role='system' vs a folded <system-reminder>) is inherited."""
    return read_policy.add_context(nr, extraction_instruction(pending_id))


def find_draft(response: NormalizedResponse) -> dict | None:
    """Extract the fenced JSON block from the assistant's reply."""
    import json
    import re
    
    text = response.text or ""
    
    # Try markdown fences first
    matches = _FENCE_RE.findall(text)
    for raw in reversed(matches):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    # Fallback: robust bracket balancer for naked JSON
    import re
    start_idx = 0
    while True:
        match = re.search(r'\{', text[start_idx:])
        if not match:
            break
            
        idx = start_idx + match.start()
        start_idx = idx + 1
        
        # Balance brackets
        depth = 0
        in_string = False
        escape = False
        
        for i in range(idx, len(text)):
            c = text[i]
            if not escape and c == '"':
                in_string = not in_string
            
            if not in_string:
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    
            escape = (c == '\\' and not escape)
            
            if depth == 0:
                raw = text[idx:i+1]
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and "knowledge" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    pass
                break
                
    return None


class DraftInvalid(Exception):
    """Schema failure — RETRYABLE, bounded, via a side call."""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def validate_draft(draft: dict) -> dict:
    """Check the model-supplied half of the ingest contract.

    Only the fields the model is allowed to provide are checked here; the
    gateway-supplied envelope (session_id, identity, visibility) is added
    later by build_ingest_payload and cannot fail validation.
    """
    if not isinstance(draft, dict):
        raise DraftInvalid("draft", "expected a JSON object")

    content = draft.get("content")
    if not isinstance(content, str) or not content.strip():
        raise DraftInvalid("content", "required, must be a non-empty string")

    knowledge = draft.get("knowledge")
    if not isinstance(knowledge, dict):
        raise DraftInvalid("knowledge", "required, must be an object")

    for field in ("title", "summary"):
        value = knowledge.get(field)
        if not isinstance(value, str) or not value.strip():
            raise DraftInvalid(f"knowledge.{field}", "required, must be a non-empty string")

    outcome = knowledge.get("outcome")
    if outcome not in OUTCOME_VALUES:
        raise DraftInvalid("knowledge.outcome", f"must be one of {list(OUTCOME_VALUES)}")

    for field in ("key_points", "next_steps", "open_questions"):
        value = knowledge.get(field)
        if value is not None and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            raise DraftInvalid(f"knowledge.{field}", "must be a list of strings")

    status = draft.get("status", DEFAULT_STATUS)
    if status not in STATUS_VALUES:
        raise DraftInvalid("status", f"must be one of {list(STATUS_VALUES)}")

    return draft


def sensitivity_flags(vault: dict, *, before: int = 0) -> dict:
    """Derive the flags /v1/ingest records, from the redaction vault.

    THIS IS THE FIELD NOTHING IN THE REPO PRODUCED BEFORE G6. The store
    accepts sensitivity_flags and writes them to redaction_audit_log
    without verifying them (store decisions-log:23 — server-side re-scanning
    was considered and explicitly rejected, so the endpoint is the only
    possible source of truth). Until the gateway filled this in, the store's
    audit trail recorded a claim nobody was making.

    `before` lets a caller count only the tokens minted while scanning THIS
    draft, rather than every token in the conversation's vault.
    """
    tokens = list(vault)[before:] if before else list(vault)
    credentials = any(t.startswith("⟦SECRET_") for t in tokens)
    pii = any(t.startswith("⟦PII_") for t in tokens)
    return {
        "contains_pii": pii,
        "contains_credentials": credentials,
        "redaction_applied": bool(tokens),
        "redaction_count": len(tokens),
    }


def build_ingest_payload(draft: dict, *, session_id: str, user_id: str,
                         department: str, team: str | None,
                         visibility: str, flags: dict,
                         agent_id: str | None = None,
                         source_system: str = "claude-code") -> dict:
    """Assemble the full /v1/ingest body: the model's draft plus the
    envelope the model is not allowed to choose."""
    knowledge = dict(draft.get("knowledge") or {})
    payload = {
        "source_system": source_system,
        "captured_by": {"user_id": user_id, **({"agent_id": agent_id} if agent_id else {})},
        "session_id": session_id,
        "content": draft["content"],
        "sensitivity_flags": flags,
        "visibility": visibility,
        "status": draft.get("status", DEFAULT_STATUS),
        "knowledge": knowledge,
        "hint": {"department": department, **({"team": team} if team else {})},
    }
    return payload


def render_for_approval(pending_id: str, payload: dict, flags: dict,
                        warnings: list[str] | None = None) -> str:
    """What the human is shown before deciding. Must be the EXACT content
    that would be stored — an approval prompt that paraphrases is not an
    approval prompt."""
    k = payload.get("knowledge") or {}
    lines = [
        f"ESDS Orgbrain — draft {pending_id} is PENDING YOUR APPROVAL. "
        "Nothing has been written to the Context Bus.",
        "",
        f"  title      {k.get('title', '')}",
        f"  summary    {k.get('summary', '')}",
        f"  outcome    {k.get('outcome', '')}",
        f"  visibility {payload.get('visibility')}  (who will be able to read it)",
        f"  author     {(payload.get('captured_by') or {}).get('user_id')} / "
        f"{(payload.get('hint') or {}).get('department')}"
        f"{'/' + (payload.get('hint') or {}).get('team') if (payload.get('hint') or {}).get('team') else ''}",
    ]
    if k.get("key_points"):
        lines.append(f"  key points {'; '.join(k['key_points'])}")
    if k.get("next_steps"):
        lines.append(f"  next steps {'; '.join(k['next_steps'])}")
    lines += [
        f"  redaction  {flags.get('redaction_count', 0)} value(s) removed "
        f"(pii={flags.get('contains_pii')}, credentials={flags.get('contains_credentials')})",
    ]
    for w in warnings or []:
        lines.append(f"  ! {w}")
    lines += [
        "",
        f"Show this to the user verbatim and tell them: type  ESDS_APPROVE {pending_id}  to save it, "
        f"ESDS_REJECT {pending_id}  to discard it. "
        f"They may add  --visibility org|department|team|private  to ESDS_APPROVE to change who can read it. "
        "Do not claim it has been saved — it has not.",
    ]
    return "\n".join(lines)
