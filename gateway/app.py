"""Gateway entrypoint.

Pipeline: detect protocol -> adapter.to_normalized -> CHECK -> READ -> WRITE
trigger -> adapter.from_normalized -> forward -> adapter.parse_response ->
CHECK restore -> WRITE apply -> log usage.

Auth model — RELAY MODE: the gateway does NOT hold or read any credential
of its own. It forwards whatever Authorization / x-api-key / anthropic-beta
headers the client already sent, unmodified. See docs/TEST-PLAN.md's
"deliberately out of scope" list — production-style dp_* key issuance
(docs/ARCHITECTURE.md §2.0) is not built here.

Run:
    uvicorn gateway.app:app --port 8080

Then, in a SEPARATE terminal (never export ANTHROPIC_BASE_URL into the shell
running an interactive Claude Code session on other projects):
    ANTHROPIC_BASE_URL=http://localhost:8080 claude

Env vars:
    DP_INJECT               "1" to enable READ-policy injection (T2/T4 test
                             scaffolding), "0"/unset to disable.
    DP_INJECT_TEXT          Text passed to the READ policy when DP_INJECT=1.
    DP_CHECK_RESTORE_STREAM ON by default ("1"). Boundary-aware token
                             restoration on the SSE streaming path. Set "0"
                             to get the old byte-identical relay back for a
                             passthrough-fidelity measurement. Note it is a
                             no-op when nothing was redacted, so ordinary
                             traffic still relays raw bytes either way.
                             (DP_WRITE_TEST is GONE — G6 gates the
                             extraction instruction on ESDS_SUBMIT in a
                             genuine human turn instead of an env flag.)
    DP_DEBUG_LOG_OUTBOUND   "1" to write the exact outbound payload bytes
                             to /tmp/dp_outbound_debug.json before sending
                             — test-only, for proving what did/didn't leave
                             the gateway. Never enable this outside testing:
                             it writes pre-redaction... no, POST-redaction
                             payload (the whole point is to prove the real
                             secret is absent), but any other sensitive
                             content in the request would also land in that
                             file in plaintext.
    DP_ARM_LABEL            Free-text tag written into each usage log line.
    DP_UPSTREAM_BASE_URL    Override upstream base URL (test-only; defaults
                             to the real Anthropic API).

    -- G6/G8, WRITE --
    DP_PENDING_DIR          Where drafts awaiting human approval are parked
                             (default /tmp/dp_pending). Nothing in that
                             directory has been written to the Context Bus.
    DP_WRITE_MAX_RETRIES    Bounded side-call retries for a schema-invalid
                             draft (default 2). Never retried in the user's
                             own thread.
    DP_DRAIN_ON_REQUEST     "1" (default) to opportunistically retry writes
                             that were queued while the bus was down, on the
                             next request from the same session. See also
                             scripts/drain_queue.py for the manual sweep.

    -- G4/G5, Context Bus --
    DP_BUS_BASE_URL         Context Bus base URL (default http://127.0.0.1:8000).
    DP_BUS_TIMEOUT          Per-call timeout for reads (default 3.0s).
    DP_BUS_INGEST_TIMEOUT   Timeout for POST /v1/ingest (default 10.0s).
    DP_IDENTITY_MAP         Path to the account_uuid -> bus identity map
                             (default store/config/account_map.json). An
                             unknown account fails CLOSED — no bus call.
    DP_SEARCH_LIMIT         Max records retrieved per ESDS_SEARCH (default 5).
    DP_SEARCH_MAX_DISTANCE  Cosine-distance ceiling for ESDS_SEARCH (1.0).
    DP_AWARENESS            "1" to enable the G5 awareness probe. Off by
                             default: it adds a bus round trip to every
                             genuine human turn.
    DP_AWARENESS_TIMEOUT    Awareness probe timeout (default 0.3s) — tight
                             on purpose, it sits in the human's request path.
    DP_AWARENESS_COOLDOWN   Seconds between probes per session (default 300).
    DP_AWARENESS_LIMIT      Max titles named by the probe (default 3).
    DP_AWARENESS_MAX_DISTANCE
                            Cosine-distance ceiling for awareness (0.62) —
                             deliberately tighter than search, because
                             awareness fires unprompted.
"""

from __future__ import annotations

import codecs
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import flows
from ._trace import install_tracer
from .policies import check as check_policy
from .policies import pii as pii_policy
from .policies import read as read_policy
from .policies import write as write_policy
from .protocol.detect import detect

install_tracer()  # no-op unless DP_TRACE=1 — see gateway/_trace.py

UPSTREAM = os.environ.get("DP_UPSTREAM_BASE_URL", "https://api.anthropic.com")

# Headers we must not forward as-is (either hop-by-hop, or we're about to
# recompute them / they don't make sense to relay verbatim).
STRIP_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding"}

DEFAULT_INJECT_TEXT = "Always end your reply with 🛂"

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FIXTURES_DIR.mkdir(exist_ok=True)

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
DOCS_DIR.mkdir(exist_ok=True)
USAGE_LOG_PATH = DOCS_DIR / "usage_log.jsonl"

DEBUG_OUTBOUND_PATH = Path("/tmp/dp_outbound_debug.json")

from fastapi.middleware.cors import CORSMiddleware
import asyncio

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

dashboard_queue = asyncio.Queue()

_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))


def strip_request_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in STRIP_REQUEST_HEADERS}


def log_usage(*, model: str | None, injected: bool, usage: dict) -> None:
    if not usage:
        return
    entry = {
        "ts": time.time(),
        "model": model,
        "injected": injected,
        "arm_label": os.environ.get("DP_ARM_LABEL", ""),
        "usage": usage,
    }
    with open(USAGE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


_debug_call_counter = 0


def maybe_log_outbound(payload: bytes) -> None:
    """Test-only: write the exact bytes about to be sent upstream to a
    UNIQUE file per call (never shared/appended-to), so a test can grep for
    a given string with zero ambiguity about which call it came from or
    risk of interleaving with another call's write. Gated behind
    DP_DEBUG_LOG_OUTBOUND."""
    global _debug_call_counter
    if os.environ.get("DP_DEBUG_LOG_OUTBOUND", "0") == "1":
        _debug_call_counter += 1
        path = DEBUG_OUTBOUND_PATH.parent / f"dp_outbound_debug_{os.getpid()}_{_debug_call_counter}.json"
        path.write_bytes(payload)


def upstream_url(path: str, request: Request) -> str:
    """Forward the query string too — a reverse proxy that silently drops
    it is wrong regardless of whether any particular query param matters
    to the upstream API today."""
    url = f"{UPSTREAM}/{path}"
    query = request.url.query
    if query:
        url += f"?{query}"
    return url


async def passthrough_raw(path: str, request: Request, raw: bytes) -> Response:
    """Unrecognized protocol, or a non-JSON body: forward byte-identical,
    no normalization, no mutation. This is docs/ARCHITECTURE.md §2.6's fail-open
    principle extended to a new failure mode — 'we don't recognize this
    wire format' gets the same treatment as a Context Bus outage.
    """
    headers = strip_request_headers(request.headers)
    headers["content-length"] = str(len(raw))
    r = await _client.post(upstream_url(path, request), content=raw, headers=headers)
    return Response(
        status_code=r.status_code,
        content=r.content,
        media_type=r.headers.get("content-type"),
    )


def _restore_text_blocks(blocks: list[dict], vault: dict) -> list[dict]:
    out = []
    for b in blocks:
        if b.get("type") == "text" and "text" in b:
            b = dict(b)
            b["text"] = check_policy.restore(b["text"], vault)
        out.append(b)
    return out


def _apply_write(nr, normalized_resp, vault: dict) -> None:
    """Response side of G6: capture the model's draft into the pending
    store. This NEVER writes to the Context Bus — approval does, and only
    a human can trigger that."""
    result = flows.handle_write_response(nr, normalized_resp, vault)
    msg = ""
    if result.get("captured"):
        msg = f"[WRITE] draft {result['pending_id']} pending approval"
    elif result.get("pending_id") and result.get("reason"):
        msg = f"[WRITE] draft {result['pending_id']} not captured: {result['reason']}"
    
    if msg:
        print(msg, flush=True)


async def _restore_sse_stream(raw_chunks, vault: dict):
    """Test-only streaming path (DP_CHECK_RESTORE_STREAM=1): parse complete
    SSE events, restore redacted tokens within content_block_delta text
    fields using a boundary-aware buffer (a token can split across chunk
    boundaries — see check_policy.StreamRestorer), then re-emit.

    This buffers at SSE-event granularity (waiting for a complete "\\n\\n"
    separator) rather than forwarding raw bytes the instant they arrive —
    a small, bounded amount of buffering, not the whole-response buffering
    T1 exists to rule out. It is opt-in specifically so the default relay
    path (used by every other test in this repo) stays byte-for-byte
    unchanged.
    """
    restorer = check_policy.StreamRestorer(vault)
    # Incremental decoder, NOT chunk.decode(errors="ignore"): a UTF-8
    # sequence split across a chunk boundary would otherwise be dropped
    # silently. The token delimiters ⟦ (U+27E6) and ⟧ (U+27E7) are 3 bytes
    # each and scripts/stub_upstream.py chunks at 4 bytes specifically to
    # land on such boundaries, so this is exercised, not theoretical.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf = ""
    async for chunk in raw_chunks:
        buf += decoder.decode(chunk)
        while "\n\n" in buf:
            event_text, buf = buf.split("\n\n", 1)
            yield (_process_sse_event(event_text, restorer) + "\n\n").encode()
    buf += decoder.decode(b"", final=True)
    if buf:
        yield buf.encode()
    tail = restorer.flush()
    if tail:
        synthetic = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": tail},
        }
        yield f"event: content_block_delta\ndata: {json.dumps(synthetic)}\n\n".encode()


def _process_sse_event(event_text: str, restorer) -> str:
    out_lines = []
    for line in event_text.split("\n"):
        if line.startswith("data: "):
            try:
                data = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                out_lines.append(line)
                continue
            delta = data.get("delta") or {}
            if data.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                delta["text"] = restorer.feed(delta["text"])
            out_lines.append("data: " + json.dumps(data))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


@app.post("/{path:path}")
async def proxy(path: str, request: Request):
    raw = await request.body()

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = None

    adapter = detect(request, body)
    if adapter is None or body is None:
        return await passthrough_raw(path, request, raw)

    nr = adapter.to_normalized(body)

    # CHECK — scans every request unconditionally (§5: "inspect every
    # request"). vault is empty (and everything downstream a no-op) unless
    # a pattern actually matches. check.py proves the mechanism with one
    # test pattern; pii.py is the first real detector suite on top of it
    # (regex + JSON-field-aware, see its module docstring). Disjoint token
    # prefixes (SECRET_ vs PII_) mean the two vaults merge with no
    # collisions, and restore()/StreamRestorer downstream are already
    # generic over any token -> value vault, so nothing else changes.
    nr, vault = check_policy.scan(nr)
    nr, pii_vault = pii_policy.scan(nr)
    vault.update(pii_vault)

    # READ — G4 (explicit ESDS_SEARCH) then G5 (awareness).
    #
    # Both run AFTER the request-side scan above and both scan whatever
    # they retrieve into the SAME `vault` before injecting it (see
    # flows.py's ordering rule). The global CHECK deliberately stays where
    # it is: moving it below READ would re-scan the entire conversation on
    # every turn and double-redact already-tokenized text.
    #
    # `vault` must be complete before StreamRestorer is constructed —
    # check.StreamRestorer snapshots max token length in __init__. It is
    # built lazily inside relay()/_restore_sse_stream, i.e. after this,
    # so retrieval-minted tokens are covered. Don't hoist it.
    nr, read_diag = await flows.handle_read(nr, vault)

    # WRITE, request side — ESDS_APPROVE / ESDS_REJECT / ESDS_SUBMIT.
    # handle_write_request contains the ONLY call to the bus's ingest
    # endpoint in this codebase, and it is reachable only from a human
    # typing ESDS_APPROVE in their own last genuine turn. The AI cannot
    # reach it: a draft it produces goes to the pending store and stops.
    nr, write_diag = await flows.handle_write_request(nr, vault)

    aware_diag = {"injected": False}
    if not read_diag["marker"] and write_diag["action"] is None:
        nr, aware_diag = await flows.handle_awareness(nr, vault)

    # DP_INJECT/DP_INJECT_TEXT test scaffolding — kept alive deliberately:
    # every T2/T4 case in docs/QA-TEST-GUIDE.md drives injection this way.
    inject_on = os.environ.get("DP_INJECT", "0") == "1"
    inject_text = os.environ.get("DP_INJECT_TEXT", DEFAULT_INJECT_TEXT)
    is_human_turn = read_policy.is_new_human_turn(nr)
    did_inject = (inject_on and is_human_turn) or read_diag["injected"] or aware_diag["injected"]
    if os.environ.get("DP_DEBUG_LOG_OUTBOUND", "0") == "1":
        last = nr.messages[-1] if nr.messages else None
        print(
            f"[DIAG] inject_on={inject_on} is_human_turn={is_human_turn} "
            f"did_inject={did_inject} last_role={getattr(last, 'role', None)!r} "
            f"last_block_types={[b.get('type') for b in getattr(last, 'content', [])] if last else None} "
            f"stream={nr.stream}",
            flush=True,
        )
    nr = read_policy.apply(nr, inject=inject_on, text=inject_text)

    # (The old DP_WRITE_TEST unconditional trigger is gone — G6 gates the
    # extraction instruction on ESDS_SUBMIT in a genuine human turn, via
    # flows.handle_write_request above.)

    out_body = adapter.from_normalized(nr)
    
    # Broadcast to dashboard
    try:
        dashboard_queue.put_nowait({
            "type": "request",
            "raw": body,
            "sanitized": out_body
        })
    except Exception:
        pass

    payload = json.dumps(out_body).encode()
    maybe_log_outbound(payload)
    headers = strip_request_headers(request.headers)
    headers["content-length"] = str(len(payload))
    url = upstream_url(path, request)
    model_name = nr.model

    if not nr.stream:
        r = await _client.post(url, content=payload, headers=headers)
        resp_json = r.json()
        normalized_resp = adapter.parse_response_json(r.status_code, resp_json)
        log_usage(model=model_name, injected=did_inject, usage=normalized_resp.usage)
        _apply_write(nr, normalized_resp, vault)
        if vault and "content" in resp_json:
            resp_json["content"] = _restore_text_blocks(resp_json["content"], vault)
        return JSONResponse(status_code=r.status_code, content=resp_json)

    # Streaming: use the lower-level send(..., stream=True) so the response
    # status/headers are available BEFORE we commit to a StreamingResponse
    # and BEFORE any body bytes are read. The `async with client.stream()`
    # context manager can't do this — entering it and then exiting before
    # streaming would close the connection, forcing a second (duplicate,
    # costly) upstream request just to learn the status code.
    req = _client.build_request("POST", url, content=payload, headers=headers)
    r = await _client.send(req, stream=True)
    real_status = r.status_code
    # G7: ON by default. Claude Code streams, so with this off the ONLY path
    # real traffic uses never restores — a ⟦PII_1⟧ token reaches the user's
    # terminal verbatim while the non-streaming path (used by nothing real)
    # restored unconditionally. Set DP_CHECK_RESTORE_STREAM=0 to get the old
    # byte-identical relay back for a passthrough-fidelity measurement.
    restore_stream = os.environ.get("DP_CHECK_RESTORE_STREAM", "1") == "1" and bool(vault)

    async def relay():
        # `parse_buf` is a SIDE buffer used only to extract usage/text for
        # logging and WRITE. It never influences what bytes are sent to the
        # client — those are forwarded verbatim, immediately.
        #
        # G7: the side buffer TEES THE RAW UPSTREAM BYTES, before any
        # restoration. Previously it accumulated from whatever `source`
        # yielded, so with restore enabled it captured RESTORED text —
        # meaning WRITE would extract a draft containing real secret values
        # and persist them into a passport, and the usage log would hold
        # them too. The client still receives restored text; only this
        # buffer stays redacted. Do not "simplify" this back into one loop.
        raw_parts: list[bytes] = []

        async def tee(chunks):
            async for chunk in chunks:
                raw_parts.append(chunk)
                yield chunk

        try:
            source = _restore_sse_stream(tee(r.aiter_raw()), vault) if restore_stream else tee(r.aiter_raw())
            async for chunk in source:
                yield chunk  # forward immediately — never whole-response-buffer
        finally:
            await r.aclose()
        # Join bytes THEN decode once: decoding per-chunk with errors="ignore"
        # silently drops a multi-byte character split across a chunk boundary,
        # and the redaction delimiters ⟦/⟧ are 3 bytes each.
        parse_buf = b"".join(raw_parts).decode("utf-8", errors="replace")
        normalized_resp = adapter.parse_response_sse(real_status, parse_buf)
        log_usage(model=model_name, injected=did_inject, usage=normalized_resp.usage)
        _apply_write(nr, normalized_resp, vault)

    return StreamingResponse(relay(), status_code=real_status, media_type="text/event-stream")

@app.get("/v1/dashboard/stream")
async def dashboard_stream(request: Request):
    """SSE endpoint for live dashboard traffic monitoring."""
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await asyncio.wait_for(dashboard_queue.get(), timeout=1.0)
                yield f"data: {json.dumps(data)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/v1/dashboard/pending")
async def dashboard_pending():
    """REST endpoint for dashboard to view pending drafts."""
    from .pending import PENDING_DIR
    drafts = []
    approved_drafts = []
    if PENDING_DIR.exists():
        for p in PENDING_DIR.glob("*.json"):
            try:
                with open(p, encoding="utf-8") as f:
                    draft = json.load(f)
                    if draft.get("status") == "pending_approval":
                        drafts.append(draft)
                    elif draft.get("status") == "approved":
                        approved_drafts.append(draft)
            except Exception:
                pass
    # sort by timestamp descending
    drafts.sort(key=lambda d: d.get("timestamp", 0), reverse=True)
    approved_drafts.sort(key=lambda d: d.get("timestamp", 0), reverse=True)
    return JSONResponse({"drafts": drafts, "approved_drafts": approved_drafts})
