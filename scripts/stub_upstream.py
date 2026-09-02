"""Stub upstream for gateway QA. Records what it received; replies per mode.

Modes (DP_STUB_MODE):
    echo      (default) "ECHO>> [role] text | ..." — the original behaviour.
              Every existing case in docs/QA-TEST-GUIDE.md depends on this
              exact shape, so it stays the default and is not changed.
    verbatim  the last human text block, byte-for-byte, with NO prefix.
              QA-FINDINGS.md #2 (High): the `ECHO>> [user] ` prefix means a
              response-side marker never lands at line start, so WRITE's
              draft detection could not be exercised at all (W2/W4/W5, C10
              were unrunnable). This mode is what makes those testable.
    draft     a canned structured knowledge draft in a fenced JSON block,
              matching POST /v1/ingest's required fields — the fixture for
              G6's detect -> validate -> DLP -> approve path.
    fixed     reply with DP_STUB_REPLY verbatim.

Other knobs:
    DP_STUB_REPLY   literal reply text for `fixed` mode.
    DP_STUB_CHUNK   SSE chunk size (default 4 — deliberately tiny so
                    redaction tokens split across chunk boundaries).
    DP_STUB_DELAY   per-chunk sleep (default 0.3 — deliberately slow so
                    whole-response buffering is obvious). Set 0 in
                    automated tests.
    DP_STUB_STATUS  return this HTTP status instead of a normal reply,
                    for exercising the failure table (401/429/500/529).

Run:  uvicorn scripts.stub_upstream:app --port 9090
"""
import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
LAST = Path("/tmp/dp_stub_last_request.json")


def _chunk_size() -> int:
    return max(1, int(os.environ.get("DP_STUB_CHUNK", "4")))


def _delay() -> float:
    return float(os.environ.get("DP_STUB_DELAY", "0.3"))


def _mode() -> str:
    return os.environ.get("DP_STUB_MODE", "echo").strip().lower()


# A draft that satisfies every required field of POST /v1/ingest. Note the
# fence opens at line start: G6 must find it there and nowhere else, so a
# fixture that indents it would silently pass a weaker test than intended.
CANNED_DRAFT = """Here is the record I extracted from this session.

```json
{
  "source_system": "claude-code",
  "content": "Chose base-URL redirect over TLS MITM for the egress checkpoint.",
  "visibility": "team",
  "status": "completed",
  "knowledge": {
    "title": "Base-URL redirect chosen over TLS MITM",
    "summary": "Interception uses ANTHROPIC_BASE_URL rather than a system proxy plus root CA, avoiding per-OS work and cert pinning failures.",
    "outcome": "decision_made",
    "key_points": ["No root CA needed", "One env var, no interface change"],
    "next_steps": ["Document the coverage boundary"]
  }
}
```
"""


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def received_text(body: dict) -> str:
    parts = []
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(f"[{m.get('role')}] {c}")
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(f"[{m.get('role')}] {b.get('text', '')}")
    return " | ".join(parts)


def last_human_text(body: dict) -> str:
    """Text blocks of the last user message, verbatim and unprefixed.

    Deliberately mirrors what the gateway's own marker predicate reads, so
    a marker the stub echoes back lands at line start exactly as a real
    model's reply would put it.
    """
    for m in reversed(body.get("messages") or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            texts = [b.get("text", "") for b in c
                     if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return ""


def reply_text(body: dict) -> str:
    mode = _mode()
    if mode == "verbatim":
        return last_human_text(body)
    if mode == "draft":
        return CANNED_DRAFT
    if mode == "fixed":
        return os.environ.get("DP_STUB_REPLY", "")
    return "ECHO>> " + received_text(body)


@app.post("/{path:path}")
async def upstream(path: str, request: Request):
    raw = await request.body()
    LAST.write_bytes(raw)

    forced = os.environ.get("DP_STUB_STATUS")
    if forced:
        code = int(forced)
        return JSONResponse(
            status_code=code,
            content={"type": "error", "error": {"type": "stub_forced", "message": f"stub forced {code}"}},
        )

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"stub": "non-json body received", "bytes": len(raw)})

    echo = reply_text(body)
    usage = {"input_tokens": 10, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}

    if not body.get("stream"):
        return JSONResponse({
            "id": "msg_stub", "type": "message", "role": "assistant",
            "model": body.get("model"),
            "content": [{"type": "text", "text": echo}],
            "stop_reason": "end_turn",
            "usage": {**usage, "output_tokens": 5},
        })

    chunk_size, delay = _chunk_size(), _delay()

    async def gen():
        yield sse("message_start", {"type": "message_start",
                                    "message": {"model": body.get("model"), "usage": usage}})
        for i in range(0, len(echo), chunk_size):
            if delay:
                await asyncio.sleep(delay)
            yield sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": echo[i:i + chunk_size]},
            })
        yield sse("message_delta", {"type": "message_delta",
                                    "delta": {"stop_reason": "end_turn"},
                                    "usage": {"output_tokens": 5}})
        yield sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream")
