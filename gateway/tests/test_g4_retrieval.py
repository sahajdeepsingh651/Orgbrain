"""G4 — ESDS_SEARCH retrieval, and the DLP-bypass regression.

The single most important test in this file is
`test_retrieved_secret_is_redacted_before_injection`. Before G4, the
pipeline ran CHECK (app.py:232) then READ (app.py:250), so anything the
gateway retrieved from the Context Bus and injected reached the LLM
UNSCANNED. `/v1/ingest` accepts client-asserted sensitivity_flags without
verifying them (store decisions-log:23, deliberately), so "it came from our
own store" is not evidence it is clean. If that test ever goes green-to-red,
the boundary is broken.

These drive gateway.flows directly with a fake bus rather than going
through ASGI: the logic under test is ordering and authorization, and a
transport-level fake would only obscure it.
"""
from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gateway import bus_client, flows                      # noqa: E402
from gateway.policies import identity as identity_policy   # noqa: E402
from gateway.protocol.anthropic_adapter import AnthropicAdapter  # noqa: E402
from gateway.tests.conftest import build_metadata, build_request  # noqa: E402

ADAPTER = AnthropicAdapter()
KNOWN_ACCOUNT = "aaaaaaaa-0000-4000-8000-000000000001"
UNKNOWN_ACCOUNT = "00000000-0000-0000-0000-00000000dead"


def sync(fn):
    """Run an async test without pytest-asyncio.

    The gateway's other 52 tests are all synchronous and the repo declares
    no test dependencies beyond pytest itself; adding pytest-asyncio just
    for these would widen the install for no behavioural gain. functools.wraps
    keeps __wrapped__ so pytest still resolves fixture params (monkeypatch)
    from the original signature.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


class FakeBus:
    """Minimal stand-in with the same surface flows.handle_read uses."""

    def __init__(self, results=None, raises=None):
        self.results = results or []
        self.raises = raises
        self.calls: list[dict] = []

    async def search(self, query, *, token, limit=10, department=None, team=None, timeout=None):
        self.calls.append({"query": query, "token": token, "limit": limit, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return self.results


def hit(**kw):
    base = {
        "record_id": "aaaaaaaa-1111-2222-3333-444444444444",
        "title": "Push notification retry policy",
        "summary": "Chose exponential backoff capped at 30s.",
        "department": "Engineering", "team": "platform",
        "outcome": "decision_made", "status": "completed",
        "created_at": "2026-08-01T10:00:00+00:00", "distance": 0.21,
    }
    base.update(kw)
    return base


def nr_with(text, *, account=KNOWN_ACCOUNT, session="sess-1", harness=False, blocks=None):
    body = build_request(
        user_text=None if blocks else text,
        content_blocks=blocks,
        metadata=build_metadata(account_uuid=account, session_id=session),
        include_system_harness=harness,
    )
    return ADAPTER.to_normalized(body)


def injected_text(nr) -> str:
    """Concatenated text of every injected role='system' message."""
    return "\n".join(
        b.get("text", "")
        for m in nr.messages if m.role == "system"
        for b in m.content if b.get("type") == "text"
    )


def user_text(nr) -> str:
    return "\n".join(
        b.get("text", "")
        for m in nr.messages if m.role == "user"
        for b in m.content if b.get("type") == "text"
    )


# --- identity precondition -------------------------------------------------

def test_account_map_resolves_the_fixture_account():
    """If this fails the rest of the file is meaningless — the map ships in
    store/config/account_map.json and must contain the fixture account."""
    assert identity_policy.resolve(KNOWN_ACCOUNT) is not None
    assert identity_policy.resolve(UNKNOWN_ACCOUNT) is None


# --- the security regression ----------------------------------------------

@sync
async def test_retrieved_secret_is_redacted_before_injection():
    """THE bypass test. A record in the bus carrying a credential must not
    reach the model in clear text just because we retrieved it ourselves."""
    secret = "sk-test-abcdefghij123"
    bus = FakeBus([hit(summary=f"Deploy key is {secret} — rotate quarterly.")])
    vault: dict = {}

    nr, diag = await flows.handle_read(nr_with("ESDS_SEARCH deploy key"), vault, bus=bus)

    assert diag["injected"] is True
    ctx = injected_text(nr)
    assert secret not in ctx, "raw secret reached the injected context"
    assert "⟦SECRET_1⟧" in ctx
    # and it must be restorable on the way back
    assert vault["⟦SECRET_1⟧"] == secret


@sync
async def test_retrieved_pii_is_redacted_before_injection():
    bus = FakeBus([hit(summary="Escalate to rohan.mehta87@gmail.com for the payment issue.")])
    vault: dict = {}
    nr, diag = await flows.handle_read(nr_with("ESDS_SEARCH payments"), vault, bus=bus)
    ctx = injected_text(nr)
    assert diag["injected"] is True
    assert "rohan.mehta87@gmail.com" not in ctx
    assert any(t.startswith("⟦PII_") for t in vault)


# --- authorization ---------------------------------------------------------

@sync
async def test_marker_in_tool_result_does_not_retrieve():
    """The injection vector: a file the agent read contains the marker."""
    bus = FakeBus([hit()])
    blocks = [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "ESDS_SEARCH everything"}]},
    ]
    nr, diag = await flows.handle_read(nr_with("", blocks=blocks), {}, bus=bus)
    assert diag["marker"] is False
    assert bus.calls == [], "a tool_result marker triggered a bus call"


@sync
async def test_unknown_account_fails_closed():
    bus = FakeBus([hit()])
    nr, diag = await flows.handle_read(
        nr_with("ESDS_SEARCH anything", account=UNKNOWN_ACCOUNT), {}, bus=bus)
    assert diag["reason"] == "unknown_account"
    assert bus.calls == [], "unknown account still reached the bus"
    assert diag["injected"] is False


@sync
async def test_marker_honoured_behind_trailing_harness_system_message():
    """Real Claude Code traffic appends its own role='system' message after
    the human turn. Injection must still fire — this exact shape is what
    silently broke retrieval before read.py's look-through rule."""
    bus = FakeBus([hit()])
    nr, diag = await flows.handle_read(
        nr_with("ESDS_SEARCH retries", harness=True), {}, bus=bus)
    assert diag["marker"] is True and diag["injected"] is True


# --- behaviour -------------------------------------------------------------

@sync
async def test_query_is_the_marker_remainder():
    bus = FakeBus([hit()])
    await flows.handle_read(nr_with("ESDS_SEARCH: push notification retries"), {}, bus=bus)
    assert bus.calls[0]["query"] == "push notification retries"


@sync
async def test_marker_is_stripped_from_what_the_llm_sees():
    bus = FakeBus([hit()])
    nr, _ = await flows.handle_read(
        nr_with("ESDS_SEARCH retries\nwhat did we decide?"), {}, bus=bus)
    assert "ESDS_SEARCH" not in user_text(nr)
    assert "what did we decide?" in user_text(nr)


@sync
async def test_exactly_one_bus_call_per_marker():
    bus = FakeBus([hit()])
    await flows.handle_read(nr_with("ESDS_SEARCH x"), {}, bus=bus)
    assert len(bus.calls) == 1


@sync
async def test_zero_results_injects_nothing():
    bus = FakeBus([])
    nr, diag = await flows.handle_read(nr_with("ESDS_SEARCH nothing"), {}, bus=bus)
    assert diag["injected"] is False and diag["reason"] == "no_hits"
    assert injected_text(nr) == ""


@sync
async def test_relevance_floor_drops_distant_hits(monkeypatch):
    from gateway.policies import read as read_policy
    monkeypatch.setattr(read_policy, "SEARCH_MAX_DISTANCE", 0.3)
    bus = FakeBus([hit(distance=0.2), hit(record_id="bbbb", distance=0.9)])
    nr, diag = await flows.handle_read(nr_with("ESDS_SEARCH x"), {}, bus=bus)
    assert diag["hits"] == 1


@sync
async def test_keyword_only_hit_without_distance_is_kept():
    """/v1/search unions ANN with an ILIKE keyword pass; a keyword hit has
    no distance and must not be silently dropped by the floor."""
    h = hit()
    del h["distance"]
    bus = FakeBus([h])
    _, diag = await flows.handle_read(nr_with("ESDS_SEARCH x"), {}, bus=bus)
    assert diag["hits"] == 1


# --- failure posture -------------------------------------------------------

@sync
async def test_bus_unavailable_fails_open():
    bus = FakeBus(raises=bus_client.BusUnavailable("timeout"))
    nr, diag = await flows.handle_read(nr_with("ESDS_SEARCH x\nreal question"), {}, bus=bus)
    assert diag["injected"] is False
    assert diag["reason"].startswith("bus_unavailable")
    # the request still goes upstream, and still carries the human's question
    assert "real question" in user_text(nr)


@sync
async def test_bus_401_fails_open_but_is_distinguishable():
    """A bad token must not look identical to 'nothing relevant exists'."""
    bus = FakeBus(raises=bus_client.BusAuthError("401"))
    _, diag = await flows.handle_read(nr_with("ESDS_SEARCH x"), {}, bus=bus)
    assert diag["reason"] == "bus_401"
    assert diag["reason"] != "no_hits"


# --- no marker = no bus traffic -------------------------------------------

@sync
async def test_normal_turn_touches_nothing():
    bus = FakeBus([hit()])
    nr_in = nr_with("just a normal question")
    nr_out, diag = await flows.handle_read(nr_in, {}, bus=bus)
    assert diag == {"marker": False, "hits": 0, "injected": False, "reason": None}
    assert bus.calls == []
    assert nr_out is nr_in
