"""G4/G5 — the READ and AWARENESS flows.

Why this module exists rather than living in app.py or read.py:

  * `policies/read.py` is pure — predicates and renderers, no I/O. Putting
    an httpx call in it would make every policy test need a network fake.
  * `app.py`'s proxy() is already the whole pipeline; adding retrieval
    orchestration inline would bury the one security-critical ordering
    decision (scan retrieved documents BEFORE injecting them) in the
    middle of transport code.

So the I/O lives here, in one function per flow, with `bus` injectable so
tests can pass a fake without patching modules.

THE ORDERING RULE, stated once:
    Documents retrieved from the Context Bus are scanned by CHECK and PII
    into the SAME vault as the request, before add_context. Retrieved
    content does not bypass the DLP boundary because it came from our own
    store — `/v1/ingest` accepts client-asserted sensitivity_flags without
    verifying them (store decisions-log:23, deliberately), so "it's ours"
    is not evidence that it is clean.

Failure posture:
    unknown account_uuid -> FAIL CLOSED (no bus call at all)
    bus unavailable      -> FAIL OPEN  (forward the request unchanged)
    bus 401              -> FAIL OPEN for reads, but LOUD in the log —
                            a misconfigured token must not look identical
                            to "nothing relevant exists".
"""
from __future__ import annotations

import os
import time

from . import bus_client, pending
from .policies import check as check_policy
from .policies import identity as identity_policy
from .policies import markers as markers_policy
from .policies import pii as pii_policy
from .policies import read as read_policy
from .policies import write as write_policy

SEARCH_MARKER = "ESDS_SEARCH"
SUBMIT_MARKER = "ESDS_SUBMIT"
APPROVE_MARKER = "ESDS_APPROVE"
REJECT_MARKER = "ESDS_REJECT"

# G6 — how many times a schema-invalid draft may be sent back to the model.
# Bounded because each retry is a full billed API call, and via a SIDE call
# (never the user's thread) because correction chatter appended to the
# conversation becomes cache prefix for every subsequent turn, forever.
MAX_DRAFT_RETRIES = int(os.environ.get("DP_WRITE_MAX_RETRIES", "2"))

# G5 — awareness is unprompted, so it must be cheap and rare. A short
# timeout because it sits in the request path of every human turn, and a
# cooldown because firing on consecutive turns turns a helpful signal into
# a banner the developer learns to ignore.
AWARENESS_TIMEOUT = float(os.environ.get("DP_AWARENESS_TIMEOUT", "0.3"))
AWARENESS_COOLDOWN_SECONDS = float(os.environ.get("DP_AWARENESS_COOLDOWN", "300"))
AWARENESS_LIMIT = int(os.environ.get("DP_AWARENESS_LIMIT", "3"))
SEARCH_LIMIT = int(os.environ.get("DP_SEARCH_LIMIT", "5"))

# session_id -> monotonic timestamp of the last awareness probe.
_awareness_last: dict[str, float] = {}


def reset_awareness_state() -> None:
    """Test hook — the cooldown is process-global by design (one gateway
    serves many sessions), so tests must be able to clear it."""
    _awareness_last.clear()


import threading

_awareness_lock = threading.Lock()
_log_lock = threading.Lock()

_failed_drafts: dict[str, str] = {} # pending_id -> failure_reason
_failed_drafts_lock = threading.Lock()

def _log(msg: str) -> None:
    with _log_lock:
        print(f"[FLOW] {msg}", flush=True)


def _scan_into_vault(text: str, vault: dict) -> str:
    """Both detector suites, into the caller's vault. Order matches
    app.py's request-side ordering (CHECK then PII) so token numbering and
    the disjoint SECRET_/PII_ prefixes behave identically on both paths."""
    return pii_policy.scan_text(check_policy.scan_text(text, vault), vault)


def _scan_hits(hits: list[dict], vault: dict) -> list[dict]:
    """Redact every free-text field of a retrieved record before it can be
    injected. Only the human-readable fields are scanned; ids and enums
    cannot carry a secret and redacting them would break attribution."""
    scanned = []
    for h in hits:
        h = dict(h)
        for field in ("title", "summary", "outcome_detail", "intent"):
            if isinstance(h.get(field), str) and h[field]:
                h[field] = _scan_into_vault(h[field], vault)
        scanned.append(h)
    return scanned


async def handle_read(nr, vault: dict, *, bus=bus_client):
    """ESDS_SEARCH: retrieve, DLP-scan, inject. Returns (nr, diag)."""
    diag: dict = {"marker": False, "hits": 0, "injected": False, "reason": None}

    if markers_policy.find_marker(nr, SEARCH_MARKER) is None:
        return nr, diag
    diag["marker"] = True

    bus_id = identity_policy.resolve(nr.metadata.get("account_uuid"))
    if bus_id is None:
        # Fail CLOSED: no identity means we cannot enforce visibility, and
        # guessing a default token would hand one user another's records.
        diag["reason"] = "unknown_account"
        _log(f"ESDS_SEARCH refused — unknown account "
             f"{identity_policy.account_hash(nr.metadata.get('account_uuid'))!r}")
        return markers_policy.strip_marker(nr, SEARCH_MARKER), diag

    query = markers_policy.marker_remainder(nr, SEARCH_MARKER)
    if not query:
        # Bare `ESDS_SEARCH` — use the rest of the turn as the query.
        query = markers_policy.last_human_turn_text(nr).replace(SEARCH_MARKER, " ").strip()
    if not query:
        diag["reason"] = "empty_query"
        return markers_policy.strip_marker(nr, SEARCH_MARKER), diag

    try:
        hits = await bus.search(query, token=bus_id.bus_token, limit=SEARCH_LIMIT)
    except bus_client.BusAuthError:
        diag["reason"] = "bus_401"
        _log("ESDS_SEARCH failed — bus rejected the token (401). "
             "Check store/config/account_map.json against api_tokens.json.")
        return markers_policy.strip_marker(nr, SEARCH_MARKER), diag
    except bus_client.BusUnavailable as exc:
        diag["reason"] = f"bus_unavailable:{exc}"
        _log(f"ESDS_SEARCH failed open — {exc}")
        return markers_policy.strip_marker(nr, SEARCH_MARKER), diag

    hits = read_policy.filter_hits(hits, read_policy.SEARCH_MAX_DISTANCE)
    diag["hits"] = len(hits)
    if not hits:
        # Zero results is a correct answer; injecting nothing is right.
        diag["reason"] = "no_hits"
        return markers_policy.strip_marker(nr, SEARCH_MARKER), diag

    # ---- the ordering rule ----
    hits = _scan_hits(hits, vault)
    rendered = read_policy.render_documents(hits)

    nr = markers_policy.strip_marker(nr, SEARCH_MARKER)
    nr = read_policy.add_context(nr, rendered)
    diag["injected"] = True
    _log(f"ESDS_SEARCH injected {len(hits)} record(s) for "
         f"{identity_policy.account_hash(nr.metadata.get('account_uuid'))}")
    return nr, diag


async def handle_awareness(nr, vault: dict, *, bus=bus_client, now=None):
    """G5 — titles-only signal on a genuine human turn. Returns (nr, diag).

    Never raises, never blocks for long, never injects a document body.
    """
    diag: dict = {"probed": False, "hits": 0, "injected": False, "reason": None}
    if os.environ.get("DP_AWARENESS", "0") != "1":
        diag["reason"] = "disabled"
        return nr, diag
    if not read_policy.is_new_human_turn(nr):
        diag["reason"] = "not_human_turn"
        return nr, diag
    # Never double up with an explicit retrieval on the same turn.
    if markers_policy.find_marker(nr, SEARCH_MARKER) is not None:
        diag["reason"] = "search_marker_present"
        return nr, diag

    bus_id = identity_policy.resolve(nr.metadata.get("account_uuid"))
    if bus_id is None:
        diag["reason"] = "unknown_account"
        return nr, diag

    query = markers_policy.last_human_turn_text(nr).strip()
    if not query:
        diag["reason"] = "empty_query"
        return nr, diag

    session_id = nr.metadata.get("session_id") or "_nosession"
    clock = now if now is not None else time.monotonic()
    
    with _awareness_lock:
        last = _awareness_last.get(session_id)
        if last is not None and (clock - last) < AWARENESS_COOLDOWN_SECONDS:
            diag["reason"] = "cooldown"
            return nr, diag
        _awareness_last[session_id] = clock

    diag["probed"] = True
    try:
        hits = await bus.search(query, token=bus_id.bus_token,
                                limit=AWARENESS_LIMIT, timeout=AWARENESS_TIMEOUT)
    except bus_client.BusError as exc:
        # Silent by design: an awareness probe that reports its own failure
        # to the developer is worse than one that quietly doesn't fire.
        diag["reason"] = f"bus_error:{type(exc).__name__}"
        return nr, diag

    hits = read_policy.filter_hits(hits, read_policy.AWARENESS_MAX_DISTANCE)
    diag["hits"] = len(hits)
    if not hits:
        diag["reason"] = "no_hits"
        return nr, diag

    # Titles only — no body, so nothing here needs DLP scanning beyond what
    # the title already went through at ingest. Scan anyway: a title is
    # free text a human wrote, and this costs nothing when nothing matches.
    rendered = _scan_into_vault(read_policy.render_awareness(hits), vault)
    nr = read_policy.add_context(nr, rendered)
    diag["injected"] = True
    return nr, diag


# ==========================================================================
# G6 — WRITE
#
# The invariant this section exists to enforce: **the AI may draft, only a
# human may persist.** Validation is not approval. There is exactly one
# call to bus.ingest() in this file, in handle_approve, and it is reachable
# only from a human typing ESDS_APPROVE in their own last genuine turn.
# ==========================================================================

def _visibility_override(line: str) -> str | None:
    """`ESDS_APPROVE ab12cd34 --visibility org` -> 'org'.

    Visibility is an access-control decision, so it is the human's, never
    the model's. Anything unrecognised is ignored rather than guessed —
    silently widening to `org` because someone typed `--visibility orgg`
    would publish a record to the whole company.
    """
    parts = line.split()
    for i, part in enumerate(parts):
        if part == "--visibility" and i + 1 < len(parts):
            candidate = parts[i + 1].strip().lower()
            return candidate if candidate in write_policy.VISIBILITY_VALUES else None
        if part.startswith("--visibility="):
            candidate = part.split("=", 1)[1].strip().lower()
            return candidate if candidate in write_policy.VISIBILITY_VALUES else None
        if part.startswith("--") and part[2:].lower() in write_policy.VISIBILITY_VALUES:
            return part[2:].lower()
    return None


async def handle_write_request(nr, vault: dict, *, bus=bus_client):
    """Request side: ESDS_APPROVE / ESDS_REJECT / ESDS_SUBMIT.

    Returns (nr, diag). diag['expect_draft'] tells the response side that
    this request asked for a draft, and diag['pending_id'] is the id the
    model was told to print.
    """
    diag: dict = {"action": None, "pending_id": None, "expect_draft": False,
                  "ingested": False, "record_id": None, "reason": None}

    session_id = nr.metadata.get("session_id") or "_nosession"
    account_uuid = nr.metadata.get("account_uuid")

    # G8 — opportunistically retry anything this session queued while the
    # bus was down. Cheap when there is nothing queued (one directory glob)
    # and it means "queued" eventually becomes "saved" without a background
    # task in a proxy that has no lifecycle events.
    if os.environ.get("DP_DRAIN_ON_REQUEST", "1") == "1":
        drained = await drain_queued_writes(session_id, account_uuid, bus=bus)
        if drained:
            diag["drained"] = drained

    # --- approve: the ONLY path that writes to the bus ---
    approve_line = markers_policy.find_marker(nr, APPROVE_MARKER)
    if approve_line is not None:
        diag["action"] = "approve"
        return await _handle_approve(nr, diag, approve_line, session_id, account_uuid, bus=bus)

    # --- reject ---
    reject_line = markers_policy.find_marker(nr, REJECT_MARKER)
    if reject_line is not None:
        diag["action"] = "reject"
        pending_id = markers_policy.find_marker_arg(nr, REJECT_MARKER)
        nr = markers_policy.strip_marker(nr, REJECT_MARKER)
        record = pending.load(pending_id, session_id=session_id) if pending_id else None
        if record is None:
            note = (f"ESDS Orgbrain: no pending draft {pending_id!r} for this session. "
                    "Nothing was discarded.")
        else:
            if record.get("status") == pending.STATUS_REJECTED:
                note = (f"ESDS Orgbrain: draft {pending_id} was ALREADY discarded. "
                        "Nothing was written to the Context Bus.")
            else:
                pending.set_status(pending_id, pending.STATUS_REJECTED)
                note = (f"The ESDS Gateway proxy intercepted the reject command. Draft {pending_id} was discarded. "
                        "Nothing was written to the Context Bus.")
                diag["pending_id"] = pending_id
        return read_policy.add_context(nr, note + " Please confirm this to the user."), diag

    # --- submit: mint the id, ask for a draft. Writes NOTHING. ---
    if markers_policy.find_marker(nr, SUBMIT_MARKER) is not None:
        diag["action"] = "submit"
        if identity_policy.resolve(account_uuid) is None:
            # Fail closed: without an identity we could not ingest later
            # anyway, so don't let the model produce a draft that can
            # never be saved.
            diag["reason"] = "unknown_account"
            nr = markers_policy.strip_marker(nr, SUBMIT_MARKER)
            return read_policy.add_context(nr, (
                "ESDS Orgbrain: this session's account is not registered with the "
                "Context Bus, so nothing can be saved. Tell the user to check their "
                "gateway identity mapping.")), diag
        pending_id = pending.new_id()
        diag["pending_id"] = pending_id
        diag["expect_draft"] = True
        nr = markers_policy.strip_marker(nr, SUBMIT_MARKER)
        nr = write_policy.inject_extraction_trigger(nr, pending_id)
        # Stash for the response side. nr.metadata is gateway-internal and
        # never serialized to the wire (see anthropic_adapter.from_normalized).
        nr.metadata["dp_pending_id"] = pending_id
        return nr, diag

    return nr, diag


async def _handle_approve(nr, diag, approve_line, session_id, account_uuid, *, bus):
    pending_id = markers_policy.find_marker_arg(nr, APPROVE_MARKER)
    nr = markers_policy.strip_marker(nr, APPROVE_MARKER)

    _log(f"Loading {pending_id} for session {session_id}")
    record = pending.load(pending_id, session_id=session_id) if pending_id else None
    if record is None:
        diag["reason"] = "no_such_pending"
        _log(f"Load failed for {pending_id}, session {session_id}")
        
        failure_reason = None
        if pending_id:
            with _failed_drafts_lock:
                failure_reason = _failed_drafts.pop(pending_id, None)
                
        if failure_reason:
            msg = (f"ESDS Orgbrain: draft {pending_id} FAILED SCHEMA VALIDATION "
                   f"and was not captured. Reason: {failure_reason}. "
                   "You must submit a new draft with the corrected schema.")
        else:
            msg = (f"ESDS Orgbrain: no pending draft {pending_id!r} for this session. "
                   "Nothing was saved. Tell the user.")
                   
        return read_policy.add_context(nr, msg), diag
    _log(f"Load succeeded for {pending_id}, session {session_id}")

    if record.get("status") == pending.STATUS_APPROVED:
        diag["reason"] = "already_approved"
        record_id = record.get("record_id", "unknown")
        return read_policy.add_context(nr, (
            f"ESDS Orgbrain: draft {pending_id} was ALREADY SAVED to the Context Bus as "
            f"record {record_id}. Tell the user it was successfully saved."
        )), diag

    bus_id = identity_policy.resolve(account_uuid)
    if bus_id is None:
        diag["reason"] = "unknown_account"
        return read_policy.add_context(nr, (
            "ESDS Orgbrain: cannot save — this session's account is not registered "
            "with the Context Bus. Nothing was written.")), diag

    draft = record["draft"]
    flags = record["sensitivity_flags"]
    visibility = _visibility_override(approve_line) or write_policy.DEFAULT_VISIBILITY

    payload = write_policy.build_ingest_payload(
        draft, session_id=session_id, user_id=bus_id.user_id,
        department=bus_id.department, team=bus_id.team,
        visibility=visibility, flags=flags,
        agent_id="claude-code",
    )
    key = pending.idempotency_key(session_id, payload)

    try:
        status, body = await bus.ingest(payload, token=bus_id.bus_token, idempotency_key=key)
    except bus_client.BusAuthError:
        diag["reason"] = "bus_401"
        pending.set_status(pending_id, pending.STATUS_PENDING, last_error="401")
        return read_policy.add_context(nr, (
            f"The ESDS Gateway proxy attempted to process the approval, but save FAILED — the Context Bus rejected this session's "
            f"credentials (401). Draft {pending_id} is still pending; nothing was written. "
            "Please confirm this failure to the user, and do not claim it was saved.")), diag
    except bus_client.BusUnavailable as exc:
        # Queue, and say so. Never report success for a write that did not
        # happen — that is the one lie this system cannot afford.
        diag["reason"] = f"queued:{exc}"
        # Freeze the exact payload the human approved. The drain must retry
        # THAT, not re-derive it — a re-derivation could differ from what
        # they saw and agreed to.
        pending.set_status(pending_id, pending.STATUS_QUEUED,
                           last_error=str(exc), ingest_payload=payload)
        return read_policy.add_context(nr, (
            f"The ESDS Gateway proxy attempted to process the approval, but the Context Bus is unreachable. Draft {pending_id} is "
            "QUEUED and has NOT been saved yet. Please confirm to the user it is queued — do not say "
            "it was saved.")), diag

    if status in (200, 201):
        record_id = (body or {}).get("record_id")
        deduped = (body or {}).get("status") == "deduplicated"
        diag["ingested"] = True
        diag["record_id"] = record_id
        diag["pending_id"] = pending_id
        pending.set_status(pending_id, pending.STATUS_APPROVED, record_id=record_id)
        note = (
            "The ESDS Gateway proxy intercepted the approval command and automatically processed it. "
            f"Draft {pending_id} was successfully SAVED to the Context Bus as "
            f"record {record_id} with visibility '{visibility}'"
            + (" (already existed — deduplicated by idempotency key)" if deduped else "")
            + ". (No tool call was required on your part). Colleagues who are permitted to see it can now retrieve it with "
              "ESDS_SEARCH. Please confirm this success to the user."
        )
        return read_policy.add_context(nr, note), diag

    diag["reason"] = f"bus_{status}"
    detail = (body or {}).get("error") or (body or {}).get("detail") or ""
    pending.set_status(pending_id, pending.STATUS_PENDING, last_error=f"{status}: {detail}")
    return read_policy.add_context(nr, (
        f"The ESDS Gateway proxy attempted to process the approval, but save FAILED ({status}: {detail}). Draft {pending_id} is still "
        "pending; nothing was written. Please confirm this failure to the user.")), diag


def handle_write_response(nr, response, vault: dict) -> dict:
    """Response side: capture the model's draft, validate, DLP-scan, park it.

    Returns a diag dict. NEVER calls the bus — that is handle_approve's
    job, and only a human can trigger it.

    Runs after the response has already streamed to the user, which is
    deliberate: buffering to hide the draft would break the non-buffered
    relay invariant, and letting the draft be visible is good for the demo.
    """
    diag: dict = {"captured": False, "pending_id": None, "reason": None, "retryable": False}
    pending_id = nr.metadata.get("dp_pending_id")
    if not pending_id:
        return diag
    diag["pending_id"] = pending_id

    draft = write_policy.find_draft(response)
    if draft is None:
        diag["reason"] = "no_draft_block"
        diag["retryable"] = True
        return diag

    try:
        write_policy.validate_draft(draft)
    except write_policy.DraftInvalid as exc:
        reason = f"invalid:{exc.field}:{exc.reason}"
        diag["reason"] = reason
        diag["retryable"] = True
        with _failed_drafts_lock:
            _failed_drafts[pending_id] = reason
        return diag

    # DLP-scan the draft BEFORE it is stored. The vault is the request's, so
    # a value already tokenized upstream keeps the same token (pii.py mints
    # per unique value, not per match).
    before = len(vault)
    scanned = _scan_draft(draft, vault)
    flags = write_policy.sensitivity_flags(vault, before=before)

    warnings = []
    if flags["contains_credentials"]:
        warnings.append("a credential-shaped value was removed from this draft")
    if flags["contains_pii"]:
        warnings.append("personal data was removed from this draft")

    pending.save(
        pending_id=pending_id,
        session_id=nr.metadata.get("session_id") or "_nosession",
        account_uuid=nr.metadata.get("account_uuid"),
        draft=scanned, sensitivity_flags=flags, warnings=warnings,
    )
    diag["captured"] = True
    import os
    _log(f"draft {pending_id} captured and PENDING approval "
         f"(redacted {flags['redaction_count']} value(s)) — nothing written to the bus. "
         f"CWD: {os.getcwd()}, PENDING_DIR: {pending.PENDING_DIR}, "
         f"SAVE_SESSION: {nr.metadata.get('session_id')}")
    return diag


def _scan_draft(draft: dict, vault: dict) -> dict:
    """Redact every free-text field of the model's draft."""
    out = dict(draft)
    if isinstance(out.get("content"), str):
        out["content"] = _scan_into_vault(out["content"], vault)
    knowledge = dict(out.get("knowledge") or {})
    for field in ("title", "summary", "intent", "outcome_detail"):
        if isinstance(knowledge.get(field), str):
            knowledge[field] = _scan_into_vault(knowledge[field], vault)
    for field in ("key_points", "next_steps", "open_questions"):
        values = knowledge.get(field)
        if isinstance(values, list):
            knowledge[field] = [
                _scan_into_vault(v, vault) if isinstance(v, str) else v for v in values
            ]
    out["knowledge"] = knowledge
    return out


# ==========================================================================
# G8 — draining queued writes
#
# handle_approve QUEUEs an approved draft when the bus is unreachable and
# tells the human it is queued. Something has to actually retry it, or
# "queued" is just a nicer word for "lost".
#
# The drain runs OPPORTUNISTICALLY at the start of a request for the same
# session rather than on a background timer: a proxy has no lifecycle
# events, a timer would need its own task and error handling, and the
# developer whose write it is will almost certainly send another request.
# The trade-off is honest and worth stating — a queued write for a session
# that never returns stays queued until someone runs scripts/drain_queue.py.
# ==========================================================================

async def drain_queued_writes(session_id: str, account_uuid: str | None, *, bus=bus_client) -> list[dict]:
    """Retry every queued write for this session. Returns one result dict
    per attempt. Never raises: a failed drain must not break the request
    that happened to trigger it."""
    results: list[dict] = []
    bus_id = identity_policy.resolve(account_uuid)
    if bus_id is None:
        return results

    for record in pending.list_queued(session_id):
        pending_id = record["pending_id"]
        payload = record.get("ingest_payload")
        if not payload:
            continue
        key = pending.idempotency_key(session_id, payload)
        try:
            status, body = await bus.ingest(payload, token=bus_id.bus_token, idempotency_key=key)
        except bus_client.BusError as exc:
            results.append({"pending_id": pending_id, "ok": False, "reason": type(exc).__name__})
            continue
        if status in (200, 201):
            record_id = (body or {}).get("record_id")
            pending.set_status(pending_id, pending.STATUS_APPROVED, record_id=record_id)
            results.append({"pending_id": pending_id, "ok": True, "record_id": record_id})
            _log(f"queued draft {pending_id} drained -> record {record_id}")
        else:
            results.append({"pending_id": pending_id, "ok": False, "reason": f"bus_{status}"})
    return results
