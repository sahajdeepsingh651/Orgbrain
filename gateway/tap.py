"""T0 — the tap.

Captures every inbound POST body to fixtures/, prints a truncated view, and
returns an error response. Does NOT forward anything upstream.

Run:
    uvicorn gateway.tap:app --port 8080

Then, in a SEPARATE terminal (never export ANTHROPIC_BASE_URL into the shell
running this assistant's own session):
    ANTHROPIC_BASE_URL=http://localhost:8080 claude
"""

import json
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FIXTURES_DIR.mkdir(exist_ok=True)

# Headers worth recording; never log Authorization or x-api-key values.
INTERESTING_HEADERS = {
    "content-type",
    "anthropic-version",
    "anthropic-beta",
    "user-agent",
}
SECRET_HEADERS = {"authorization", "x-api-key"}

app = FastAPI()


@app.post("/{path:path}")
async def tap(path: str, request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {"_unparsed_raw_len": len(raw)}

    headers_seen = {
        k: v for k, v in request.headers.items() if k.lower() in INTERESTING_HEADERS
    }
    auth_mode = "none"
    for k in request.headers:
        lk = k.lower()
        if lk == "x-api-key":
            auth_mode = "api-key (x-api-key)"
        elif lk == "authorization":
            val = request.headers[k]
            auth_mode = (
                "oauth (Authorization: Bearer sk-ant-oat...)"
                if "oat" in val
                else "authorization-bearer (non-oauth-looking)"
            )

    ts = time.strftime("%Y%m%dT%H%M%S")
    fname = FIXTURES_DIR / f"{ts}_{path.replace('/', '_')}.json"
    fname.write_text(json.dumps(body, indent=2), encoding="utf-8")

    print(f"\n{'=' * 70}")
    print(f"POST /{path}")
    print(f"auth mode        : {auth_mode}")
    print(f"headers (safe)   : {headers_seen}")
    print(f"saved to         : {fname}")
    msgs = body.get("messages", [])
    print(f"messages count   : {len(msgs)}")
    if msgs:
        last = msgs[-1]
        content = last.get("content")
        shape = "string" if isinstance(content, str) else "list-of-blocks"
        print(f"last message role: {last.get('role')}  content shape: {shape}")
        if isinstance(content, list):
            types = [b.get("type") for b in content]
            print(f"last message block types: {types}")
    sys_field = body.get("system")
    if isinstance(sys_field, list):
        cache_marks = sum(1 for b in sys_field if "cache_control" in b)
        print(f"system blocks: {len(sys_field)}, with cache_control: {cache_marks}")
    # Count cache_control breakpoints anywhere in the body (system + messages).
    breakpoints = json.dumps(body).count('"cache_control"')
    print(f"total cache_control occurrences in body: {breakpoints}")
    print(json.dumps(body, indent=2)[:2000])
    print(f"{'=' * 70}\n")

    return JSONResponse(
        status_code=200,
        content={
            "type": "error",
            "error": {
                "type": "tap_intercepted",
                "message": (
                    "This is the T0 tap — it captures and does not forward. "
                    f"Body saved to {fname.name}."
                ),
            },
        },
    )
