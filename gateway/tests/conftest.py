"""Pytest harness for the gateway — Layer 1 tests.

No Docker, no live services. A FastAPI app impersonates the Context Bus
in-process; a monkeypatched httpx transport impersonates the upstream
(Anthropic) so tests can assert what the gateway SENT without making a
network call. Designed so the security boundary (redaction bypass,
marker authorization, write-stop-ship) can never regress silently.

This module (conftest.py) builds the fixtures. Test modules import
broken-down helper factories (build_request, request_via_gateway, ...).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Make the repo root importable whether pytest is run from repo root or
# from gateway/tests/. `rootdir` is set by pytest's ini from conftest path.
import os
import sys

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# The identity map the gateway reads by default (store/config/account_map.json)
# is developer-local and gitignored — it holds real account UUIDs. Tests must
# never depend on it, or they pass here and fail on a fresh clone. Point the
# policy at a committed, entirely-synthetic map instead.
os.environ.setdefault("DP_IDENTITY_MAP", str(Path(__file__).parent / "data" / "account_map.test.json"))

from gateway.policies import identity as _identity  # noqa: E402
_identity.reload_map()


# ---- Fake bus -------------------------------------------------------------

class FakeBus:
    """In-memory stand-in for the Context Bus. Records each call so tests
    can assert exactly what left the gateway. Returns canned responses
    keyed by endpoint; raises can be configured to simulate failures."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.search_results: list[dict] = []
        self.search_status: int = 200
        self.ingest_status: int = 201
        self.ingest_latency: float = 0.0
        self.raise_on_search: bool = False
        self.raise_on_ingest: bool = False
        self.ingest_calls: list[dict] = []

    def reset(self) -> None:
        self.calls.clear()
        self.search_results = []
        self.search_status = 200
        self.ingest_status = 201
        self.ingest_latency = 0.0
        self.raise_on_search = False
        self.raise_on_ingest = False
        self.ingest_calls.clear()


@pytest.fixture
def fake_bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def bus_app(fake_bus: FakeBus) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/search")
    async def search(request: Request) -> JSONResponse:
        fake_bus.calls.append({"path": "/v1/search", "query": dict(request.query_params), "headers": dict(request.headers)})
        if fake_bus.raise_on_search:
            return JSONResponse({"error": "bus internal error"}, status_code=500)
        # Return canned results; let tests set them
        return JSONResponse({"results": fake_bus.search_results}, status_code=fake_bus.search_status)

    @app.post("/v1/ingest")
    async def ingest(request: Request) -> JSONResponse:
        body = await request.json()
        fake_bus.calls.append({"path": "/v1/ingest", "body": body, "headers": dict(request.headers)})
        fake_bus.ingest_calls.append({"body": body, "idempotency_key": request.headers.get("idempotency-key")})
        if fake_bus.raise_on_ingest:
            return JSONResponse({"error": "bus internal error"}, status_code=500)
        return JSONResponse({"record_id": "rec-test-1", "gold_ref": "/v1/knowledge/rec-test-1", "status": "committed"}, status_code=fake_bus.ingest_status)

    return app


# ---- Stub upstream --------------------------------------------------------

@dataclass
class StubUpstream:
    """Records the last body and returns a canned SSE/non-SSE response.
    Tests set `.response_text` to control what the upstream 'echoes back'."""
    received: list[dict] = field(default_factory=list)
    response_text: str = "ok"
    stream: bool = True
    status_code: int = 200


@pytest.fixture
def stub_upstream() -> StubUpstream:
    return StubUpstream()


def make_upstream_transport(stub: StubUpstream) -> httpx.MockTransport:
    """The gateway POSTs to the upstream via the configured client. Tests
    set the client's transport with this so no network call happens."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        stub.received.append(body)
        if stub.stream:
            # Build a real SSE stream of the response_text.
            text = stub.response_text

            def sse_stream():
                yield f"event: message_start\ndata: {json.dumps({'type':'message_start','message':{'model':body.get('model'),'usage':{'input_tokens':10,'output_tokens':0}}})}\n\n".encode()
                i = 0
                while i < len(text):
                    # Small chunks so split-across-boundary restoration can be tested.
                    chunk = text[i:i+4]
                    yield f"event: content_block_delta\ndata: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':chunk}})}\n\n".encode()
                    i += 4
                yield f"event: message_delta\ndata: {json.dumps({'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':5}})}\n\n".encode()
                yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n".encode()

            return httpx.Response(status_code=stub.status_code, content=b"".join(sse_stream()), headers={"content-type": "text/event-stream"})
        return httpx.Response(
            status_code=stub.status_code,
            json={
                "id": "msg_stub", "type": "message", "role": "assistant",
                "model": body.get("model"),
                "content": [{"type": "text", "text": stub.response_text}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    return httpx.MockTransport(handler)


# ---- Gateway client ------------------------------------------------------

@pytest.fixture
def gateway_env(monkeypatch, tmp_path):
    """Set env vars to make the gateway testable. Returns a dict-like
    namespace so tests can mutate per-test."""
    # Force fail-open on upstream/bus and disable the test-only write flag.
    upstream_url = "http://stub-upstream.local"
    monkeypatch.setenv("DP_UPSTREAM_BASE_URL", upstream_url)
    monkeypatch.setenv("DP_DEBUG_LOG_OUTBOUND", "0")
    monkeypatch.setenv("DP_INJECT", "0")
    # Streaming restore on by default so redaction/passthrough tests can
    # see what the CLIENT receives.
    monkeypatch.setenv("DP_CHECK_RESTORE_STREAM", "1")


async def _run_gateway_request(
    body: dict,
    monkeypatch,
    stub_upstream: StubUpstream,
    bus_app: FastAPI,
    *,
    path: str = "/v1/messages",
    headers: dict | None = None,
) -> tuple[dict, httpx.Response]:
    """Drive the gateway's proxy() handler directly via ASGI so no real
    HTTP listen/port is needed. Returns (stub_received, client_received_json)."""
    raise NotImplementedError  # placeholder; tests use httpx + ASGITransport


@pytest.fixture
def drive_gateway(monkeypatch, stub_upstream, bus_app):
    """Returns an async function that POSTs to the gateway via ASGI transport.
    The upstream is mocked via make_upstream_transport; the bus is mocked
    via a second ASGI transport Mount — the gateway's bus_client will be
    monkeypatched per test to use the bus_app's ASGITransport."""
    import httpx
    from gateway import app as gateway_app_module

    async def _drive(body: dict, path: str = "/v1/messages", method: str = "POST", stream: bool = False) -> httpx.Response:
        # Rebuild gateway httpx clients with stubs BEFORE importing the app.
        upstream_transport = make_upstream_transport(stub_upstream)
        bus_client_transport = httpx.ASGITransport(app=bus_app)
        monkeypatch.setattr(
            gateway_app_module._client, "_transport", upstream_transport, raising=False
        )
        # monkeypatch the upstream URL host so the stub transport matches.
        monkeypatch.setattr(
            gateway_app_module, "UPSTREAM", "http://stub-upstream.local", raising=True
        )
        # Bus client is created later by us; tests that exercise bus flows
        # will patch it themselves. Drive the gateway via ASGI.
        asgi_transport = httpx.ASGITransport(app=gateway_app_module.app)
        async with httpx.AsyncClient(transport=asgi_transport, base_url="http://gateway.local") as client:
            req_headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
            req_headers.update((headers or {}))
            url = f"http://gateway.local{path}"
            if stream and body.get("stream"):
                # collect the streamed body
                async with client.stream("POST", url, json=body, headers=req_headers) as r:
                    content = b""
                    async for chunk in r.aiter_raw():
                        content += chunk
                    headers_out = dict(r.headers)
                    status = r.status_code
                return httpx.Response(status_code=status, content=content, headers=headers_out)
            return await client.post(url, json=body, headers=req_headers)

    return _drive


# ---- Helpers -------------------------------------------------------------

def build_request(*, user_text: str | None = None, content_blocks: list[dict] | None = None,
                  metadata: dict | None = None, model: str = "claude-opus-5-1",
                  stream: bool = True, include_system_harness: bool = False) -> dict:
    """Build an Anthropic-style /v1/messages body. If include_system_harness
    is True, append the trailing role:'system' message Claude Code adds —
    so is_new_human_turn's look-through rule can be exercised."""
    if user_text is not None:
        blocks = [{"type": "text", "text": user_text}]
    elif content_blocks is not None:
        blocks = content_blocks
    else:
        blocks = [{"type": "text", "text": "hi"}]
    messages = [{"role": "user", "content": blocks}]
    if include_system_harness:
        messages.append({"role": "system", "content": [{"type": "text", "text": "harness: skills loaded"}]})
    body: dict[str, Any] = {"model": model, "max_tokens": 1024, "messages": messages, "stream": stream}
    if metadata is not None:
        body["metadata"] = metadata
    return body


def build_metadata(account_uuid: str = "aaaaaaaa-0000-4000-8000-000000000001",
                   session_id: str = "11111111-0000-4000-8000-0000000000a1",
                   device_id: str = "0000000000000000000000000000000000000000000000000000000000000000",
                   extra: dict | None = None) -> dict:
    """Build the JSON-string user_id shape Claude Code actually sends.
    Two of three real fixtures share this exact session_id — confirms a
    stable session within one session and distinct across sessions."""
    inner = {"device_id": device_id, "account_uuid": account_uuid, "session_id": session_id}
    if extra:
        inner.update(extra)
    return {"user_id": json.dumps(inner)}
