"""G5 — the awareness probe.

Awareness exists to solve the problem the whole project is named for: a
developer cannot ask for a colleague's session if they don't know it
exists. But it fires UNPROMPTED on every genuine human turn, so the
constraints matter more than the feature:

  * titles and a count only — never a document body. Injecting bodies
    unprompted is the blind injection this design rejects, on the grounds
    that wrong context is worse than no context.
  * a tighter relevance floor than ESDS_SEARCH, because the human did not
    ask and cannot correct a bad guess.
  * a cooldown, or a helpful signal becomes a banner people learn to skip.
  * silent on failure. A probe that reports its own errors is worse than
    one that quietly doesn't fire.
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
from gateway.protocol.anthropic_adapter import AnthropicAdapter  # noqa: E402
from gateway.tests.conftest import build_metadata, build_request  # noqa: E402
from gateway.tests.test_g4_retrieval import (              # noqa: E402
    KNOWN_ACCOUNT, UNKNOWN_ACCOUNT, FakeBus, hit, injected_text, nr_with, sync,
)

ADAPTER = AnthropicAdapter()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """The cooldown map is process-global by design (one gateway serves many
    sessions), so it must be cleared between tests."""
    flows.reset_awareness_state()
    monkeypatch.setenv("DP_AWARENESS", "1")
    yield
    flows.reset_awareness_state()


# --- gating ----------------------------------------------------------------

@sync
async def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DP_AWARENESS", raising=False)
    bus = FakeBus([hit()])
    _, diag = await flows.handle_awareness(nr_with("how do we do retries?"), {}, bus=bus)
    assert diag["reason"] == "disabled"
    assert bus.calls == []


@sync
async def test_fires_on_a_genuine_human_turn():
    bus = FakeBus([hit()])
    nr, diag = await flows.handle_awareness(nr_with("how do we do retries?"), {}, bus=bus)
    assert diag["probed"] is True and diag["injected"] is True
    assert len(bus.calls) == 1


@sync
async def test_never_fires_on_a_tool_loop_hop():
    """A user turn whose content is entirely tool_result is the agent
    continuing a loop, not a person asking something — probing there would
    hit the bus many times per human turn."""
    bus = FakeBus([hit()])
    blocks = [{"type": "tool_result", "tool_use_id": "t1",
               "content": [{"type": "text", "text": "file contents"}]}]
    _, diag = await flows.handle_awareness(nr_with("", blocks=blocks), {}, bus=bus)
    assert diag["reason"] == "not_human_turn"
    assert bus.calls == []


@sync
async def test_does_not_double_up_with_explicit_search():
    bus = FakeBus([hit()])
    _, diag = await flows.handle_awareness(nr_with("ESDS_SEARCH retries"), {}, bus=bus)
    assert diag["reason"] == "search_marker_present"
    assert bus.calls == []


@sync
async def test_unknown_account_does_not_probe():
    bus = FakeBus([hit()])
    _, diag = await flows.handle_awareness(
        nr_with("a question", account=UNKNOWN_ACCOUNT), {}, bus=bus)
    assert diag["reason"] == "unknown_account"
    assert bus.calls == []


# --- cooldown --------------------------------------------------------------

@sync
async def test_cooldown_suppresses_the_second_turn():
    bus = FakeBus([hit()])
    _, first = await flows.handle_awareness(nr_with("q1", session="s-A"), {}, bus=bus, now=1000.0)
    _, second = await flows.handle_awareness(nr_with("q2", session="s-A"), {}, bus=bus, now=1010.0)
    assert first["probed"] is True
    assert second["probed"] is False and second["reason"] == "cooldown"
    assert len(bus.calls) == 1


@sync
async def test_cooldown_expires():
    bus = FakeBus([hit()])
    await flows.handle_awareness(nr_with("q1", session="s-B"), {}, bus=bus, now=1000.0)
    _, later = await flows.handle_awareness(
        nr_with("q2", session="s-B"), {}, bus=bus,
        now=1000.0 + flows.AWARENESS_COOLDOWN_SECONDS + 1)
    assert later["probed"] is True
    assert len(bus.calls) == 2


@sync
async def test_cooldown_is_per_session():
    """Two developers must not starve each other's probes."""
    bus = FakeBus([hit()])
    await flows.handle_awareness(nr_with("q", session="s-1"), {}, bus=bus, now=1000.0)
    _, other = await flows.handle_awareness(nr_with("q", session="s-2"), {}, bus=bus, now=1001.0)
    assert other["probed"] is True
    assert len(bus.calls) == 2


@sync
async def test_cooldown_is_recorded_even_when_the_bus_fails():
    """Otherwise a down bus turns every turn into a fresh timeout — the
    developer pays the latency repeatedly for nothing."""
    bus = FakeBus(raises=bus_client.BusUnavailable("timeout"))
    await flows.handle_awareness(nr_with("q", session="s-C"), {}, bus=bus, now=1000.0)
    _, second = await flows.handle_awareness(nr_with("q", session="s-C"), {}, bus=bus, now=1005.0)
    assert second["reason"] == "cooldown"
    assert len(bus.calls) == 1


# --- content constraints ---------------------------------------------------

@sync
async def test_injects_titles_but_never_a_body():
    body_text = "Chose exponential backoff capped at 30s with a jittered first retry."
    bus = FakeBus([hit(title="Push retry policy", summary=body_text)])
    nr, diag = await flows.handle_awareness(nr_with("retry question"), {}, bus=bus)
    ctx = injected_text(nr)
    assert diag["injected"] is True
    assert "Push retry policy" in ctx
    assert body_text not in ctx, "awareness leaked a document body"
    assert "ESDS_SEARCH" in ctx, "the signal must tell the human how to retrieve"


@sync
async def test_tighter_floor_than_search():
    """A hit good enough for an explicit search can still be too weak to
    justify an unprompted banner."""
    from gateway.policies import read as read_policy
    assert read_policy.AWARENESS_MAX_DISTANCE < read_policy.SEARCH_MAX_DISTANCE
    bus = FakeBus([hit(distance=read_policy.AWARENESS_MAX_DISTANCE + 0.1)])
    _, diag = await flows.handle_awareness(nr_with("q"), {}, bus=bus)
    assert diag["hits"] == 0 and diag["injected"] is False


@sync
async def test_no_hits_injects_nothing():
    bus = FakeBus([])
    nr, diag = await flows.handle_awareness(nr_with("q"), {}, bus=bus)
    assert diag["injected"] is False
    assert injected_text(nr) == ""


@sync
async def test_uses_the_tight_timeout():
    bus = FakeBus([hit()])
    await flows.handle_awareness(nr_with("q"), {}, bus=bus)
    assert bus.calls[0]["timeout"] == flows.AWARENESS_TIMEOUT
    assert bus.calls[0]["timeout"] <= 1.0, "awareness sits in the human's request path"


# --- failure posture -------------------------------------------------------

@sync
async def test_bus_failure_is_silent_and_leaves_the_request_alone():
    bus = FakeBus(raises=bus_client.BusUnavailable("timeout"))
    nr_in = nr_with("a real question")
    nr_out, diag = await flows.handle_awareness(nr_in, {}, bus=bus)
    assert diag["injected"] is False
    assert nr_out is nr_in, "a failed probe must not alter the request"


@sync
async def test_bus_401_is_also_silent():
    bus = FakeBus(raises=bus_client.BusAuthError("401"))
    _, diag = await flows.handle_awareness(nr_with("q"), {}, bus=bus)
    assert diag["injected"] is False
    assert diag["reason"].startswith("bus_error")
