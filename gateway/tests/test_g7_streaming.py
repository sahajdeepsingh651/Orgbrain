"""G7 — streaming restoration, and the raw side-buffer tee.

Two coupled defects this closes:

  1. DP_CHECK_RESTORE_STREAM defaulted to "0" while Claude Code streams, so
     on the ONLY path real traffic uses, redaction was one-way: a ⟦PII_1⟧
     token reached the user's terminal verbatim. (The non-streaming path,
     which nothing real uses, restored unconditionally.)
  2. When restore WAS enabled, the side buffer accumulated from the
     RESTORED stream — so WRITE would extract a draft containing real
     secret values and persist them, and the usage log would hold them too.

The tee test is the important one: the client must see the restored value
while the side buffer stays redacted. Those two requirements pull in
opposite directions, which is exactly why it needs a test.
"""
from __future__ import annotations

import asyncio
import functools
import json
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gateway import app as gw                              # noqa: E402
from gateway.policies import check as check_policy         # noqa: E402
from gateway.tests.conftest import build_metadata, build_request  # noqa: E402
from gateway.tests.test_g4_retrieval import sync           # noqa: E402

SECRET = "sk-test-streamsecret9"


def sse_bytes(text: str, chunk: int = 3) -> bytes:
    """An SSE stream carrying `text` as several text_delta events.

    Split by CHARACTERS here — a real upstream emits well-formed events
    containing whole characters. Byte-level splitting is what the NETWORK
    does, and that is modelled separately by `_byte_chunks`, which slices
    the finished byte stream at arbitrary offsets. Splitting characters
    here instead would corrupt the payload before the gateway ever saw it
    and would test nothing.
    """
    parts = [
        f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'model': 'claude-sonnet-5', 'usage': {'input_tokens': 10}}})}\n\n".encode()
    ]
    for i in range(0, len(text), chunk):
        piece = text[i:i + chunk]
        parts.append(
            f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': piece}})}\n\n".encode()
        )
    parts.append(
        f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 5}})}\n\n".encode()
    )
    parts.append(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
    return b"".join(parts)


def sse_text(raw: str) -> str:
    """Concatenate the text_delta payloads of an SSE stream.

    Assert on THIS, not on raw bytes. `_process_sse_event` re-serialises
    each event with json.dumps, whose default ensure_ascii=True turns
    non-ASCII into \\uXXXX escapes — semantically identical JSON, which
    every client decodes correctly, but not byte-identical. A raw-substring
    assertion would be testing json.dumps' escaping policy, not the gateway.
    """
    out = []
    for block in raw.split("\n\n"):
        for line in block.split("\n"):
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            delta = data.get("delta") or {}
            if data.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                out.append(delta.get("text", ""))
    return "".join(out)


# --- the restore generator, directly -------------------------------------

async def _drain(gen) -> str:
    out = b""
    async for chunk in gen:
        out += chunk
    return out.decode("utf-8")


def _byte_chunks(data: bytes, size: int):
    async def gen():
        for i in range(0, len(data), size):
            yield data[i:i + size]
    return gen()


@sync
async def test_token_split_across_chunks_is_restored():
    """StreamRestorer holds back a partial suffix; this proves the wiring
    around it works at every offset, not just lucky ones."""
    vault = {"⟦SECRET_1⟧": SECRET}
    stream = sse_bytes("the key is ⟦SECRET_1⟧ ok")
    for size in (1, 2, 3, 4, 5, 7, 13):
        text = sse_text(await _drain(gw._restore_sse_stream(_byte_chunks(stream, size), vault)))
        assert SECRET in text, f"restore failed at chunk size {size}"
        assert "⟦SECRET_1⟧" not in text, f"token survived at chunk size {size}"


@sync
async def test_multibyte_character_split_across_chunks_survives():
    """⟦ is U+27E6, three bytes. Decoding each chunk independently with
    errors='ignore' silently dropped a byte here; an incremental decoder
    does not. Uses a token the restorer will NOT rewrite, so what is being
    tested is the decode, not the replacement."""
    vault = {"⟦SECRET_9⟧": "unused"}
    payload = "prefix ⟦SECRET_1⟧ and 日本語 suffix"
    stream = sse_bytes(payload)
    for size in (1, 2, 3, 5):
        text = sse_text(await _drain(gw._restore_sse_stream(_byte_chunks(stream, size), vault)))
        assert "⟦SECRET_1⟧" in text, f"delimiter mangled at chunk size {size}"
        assert "日本語" in text, f"CJK mangled at chunk size {size}"
        assert "�" not in text, f"replacement char appeared at chunk size {size}"


@sync
async def test_empty_vault_is_a_passthrough():
    stream = sse_bytes("nothing to restore here")
    text = sse_text(await _drain(gw._restore_sse_stream(_byte_chunks(stream, 4), {})))
    assert "nothing to restore here" in text


# --- through the real gateway --------------------------------------------

def _streaming_response(stream_bytes: bytes, wire_chunk: int = 3) -> httpx.Response:
    """A MockTransport response the gateway can actually STREAM.

    httpx.Response(content=<bytes>) is already-read, so aiter_raw() raises
    StreamConsumed. An async byte-iterator gives a real stream, and slicing
    at `wire_chunk` bytes reproduces the arbitrary network boundaries that
    split multi-byte characters and redaction tokens.
    """
    async def body():
        for i in range(0, len(stream_bytes), wire_chunk):
            yield stream_bytes[i:i + wire_chunk]

    return httpx.Response(200, content=body(),
                          headers={"content-type": "text/event-stream"})


def _mount_upstream(monkeypatch, stream_bytes: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(stream_bytes)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(gw, "_client", client)
    monkeypatch.setattr(gw, "UPSTREAM", "http://upstream.local")
    return client


async def _post_stream(body: dict) -> tuple[int, str]:
    transport = httpx.ASGITransport(app=gw.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.local") as c:
        async with c.stream("POST", "/v1/messages", json=body,
                            headers={"content-type": "application/json"}) as r:
            raw = b""
            async for chunk in r.aiter_raw():
                raw += chunk
            return r.status_code, raw.decode("utf-8")


@sync
async def test_client_receives_the_restored_value_on_the_streaming_path(monkeypatch):
    """The headline fix: streaming restore is ON by default, so the human
    sees their real value rather than ⟦SECRET_1⟧."""
    monkeypatch.delenv("DP_CHECK_RESTORE_STREAM", raising=False)
    monkeypatch.setenv("DP_AWARENESS", "0")
    _mount_upstream(monkeypatch, sse_bytes("you gave me ⟦SECRET_1⟧ just now"))

    body = build_request(user_text=f"my key is {SECRET}",
                         metadata=build_metadata(), stream=True)
    status, raw = await _post_stream(body)
    text = sse_text(raw)
    assert status == 200
    assert SECRET in text, "client did not receive the restored value"
    assert "⟦SECRET_1⟧" not in text, "a redaction token leaked to the client"


@sync
async def test_outbound_payload_is_redacted_while_client_sees_plaintext(monkeypatch):
    """Both halves of the contract in one run: upstream gets the token, the
    human gets the value."""
    monkeypatch.delenv("DP_CHECK_RESTORE_STREAM", raising=False)
    monkeypatch.setenv("DP_AWARENESS", "0")
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _streaming_response(sse_bytes("echo ⟦SECRET_1⟧ done"))

    monkeypatch.setattr(gw, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(gw, "UPSTREAM", "http://upstream.local")

    body = build_request(user_text=f"my key is {SECRET}",
                         metadata=build_metadata(), stream=True)
    _, raw = await _post_stream(body)
    text = sse_text(raw)

    # ensure_ascii=False so this assertion inspects the payload, not
    # our own re-serialisation of it.
    outbound = json.dumps(sent[0], ensure_ascii=False)
    assert SECRET not in outbound, "raw secret left the gateway"
    assert "⟦SECRET_1⟧" in outbound
    assert SECRET in text, "client did not get the value back"


@sync
async def test_side_buffer_tees_raw_bytes_not_restored_ones(monkeypatch):
    """THE tee test. WRITE and the usage log read the side buffer; if it
    accumulated restored text, an approved draft would carry real secrets
    into the Context Bus."""
    monkeypatch.delenv("DP_CHECK_RESTORE_STREAM", raising=False)
    monkeypatch.setenv("DP_AWARENESS", "0")
    _mount_upstream(monkeypatch, sse_bytes("reply mentioning ⟦SECRET_1⟧ here"))

    seen: list[str] = []
    real_parse = gw.AnthropicAdapter.parse_response_sse if hasattr(gw, "AnthropicAdapter") else None

    from gateway.protocol import anthropic_adapter as aa
    original = aa.AnthropicAdapter.parse_response_sse

    def spy(self, status_code, sse_text):
        seen.append(sse_text)
        return original(self, status_code, sse_text)

    monkeypatch.setattr(aa.AnthropicAdapter, "parse_response_sse", spy)

    body = build_request(user_text=f"my key is {SECRET}",
                         metadata=build_metadata(), stream=True)
    _, client_raw = await _post_stream(body)
    client_text = sse_text(client_raw)

    assert seen, "parse_response_sse was never called"
    side_buffer = sse_text(seen[0])
    assert SECRET not in side_buffer, "SIDE BUFFER CAPTURED THE REAL SECRET"
    assert "⟦SECRET_1⟧" in side_buffer, "side buffer should hold the redacted token"
    # ...while the client still got the real thing
    assert SECRET in client_text


@sync
async def test_restore_can_still_be_disabled_for_fidelity_measurement(monkeypatch):
    monkeypatch.setenv("DP_CHECK_RESTORE_STREAM", "0")
    monkeypatch.setenv("DP_AWARENESS", "0")
    _mount_upstream(monkeypatch, sse_bytes("you gave me ⟦SECRET_1⟧ just now"))
    body = build_request(user_text=f"my key is {SECRET}",
                         metadata=build_metadata(), stream=True)
    _, raw = await _post_stream(body)
    text = sse_text(raw)
    assert "⟦SECRET_1⟧" in text, "opt-out should give the old byte-identical relay"
    assert SECRET not in text
