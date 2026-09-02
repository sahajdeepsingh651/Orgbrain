"""G8 — failure semantics.

Two jobs here.

1. The POLICY table must stay exhaustive. Adding a Flow or a Failure
   without deciding what it does is the drift this module exists to
   prevent, and `test_policy_is_exhaustive` turns that into a failing test
   rather than a surprise at 2am.

2. Each flow's OBSERVED behaviour must match the table. A table that says
   one thing while the code does another is worse than no table, so these
   tests drive the real flows and check the outcome against `decide()`.
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

from gateway import bus_client, flows, pending                   # noqa: E402
from gateway.failure import (                                    # noqa: E402
    POLICY, Disposition, Failure, Flow, decide, is_silent, reports_success,
)
from gateway.tests.test_g4_retrieval import (                    # noqa: E402
    KNOWN_ACCOUNT, UNKNOWN_ACCOUNT, injected_text, nr_with, sync,
)
from gateway.tests.test_g6_write import (                        # noqa: E402
    FakeBus, VALID_DRAFT, nr, response, submit_and_capture,
)


@pytest.fixture(autouse=True)
def _isolated_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(pending, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setenv("DP_AWARENESS", "1")
    flows.reset_awareness_state()
    yield
    flows.reset_awareness_state()


# ==========================================================================
# 1. The table itself
# ==========================================================================

def test_policy_is_exhaustive():
    """Every Flow x Failure combination has an explicit decision."""
    missing = [(f, e) for f in Flow for e in Failure if (f, e) not in POLICY]
    assert not missing, f"undecided failure modes: {missing}"


def test_no_extra_entries():
    extra = [k for k in POLICY if k[0] not in set(Flow) or k[1] not in set(Failure)]
    assert not extra


def test_dlp_failure_always_fails_closed():
    """The one rule that may never be weakened: a redaction that could not
    be performed may not be skipped."""
    for flow in Flow:
        assert decide(flow, Failure.DLP) is Disposition.FAIL_CLOSED, flow


def test_a_write_that_did_not_happen_is_never_success():
    for disposition in Disposition:
        assert reports_success(disposition) is False


def test_unknown_identity_never_fails_open_on_a_bus_touching_flow():
    """Guessing a default token would hand one user another's records."""
    for flow in (Flow.READ, Flow.WRITE_SUBMIT, Flow.WRITE_APPROVE):
        assert decide(flow, Failure.UNKNOWN_IDENTITY) is Disposition.FAIL_CLOSED, flow


def test_approve_queues_rather_than_losing_the_write():
    assert decide(Flow.WRITE_APPROVE, Failure.BUS_UNAVAILABLE) is Disposition.QUEUE


def test_approve_does_not_retry_a_schema_failure_through_the_model():
    """By approval time a human has agreed to specific content; silently
    re-drafting would store something they never saw."""
    assert decide(Flow.WRITE_APPROVE, Failure.BUS_SCHEMA) is Disposition.SURFACE
    assert decide(Flow.WRITE_SUBMIT, Failure.DRAFT_INVALID) is Disposition.RETRY_BOUNDED


def test_awareness_is_the_only_silent_flow():
    assert is_silent(Flow.AWARENESS)
    assert not any(is_silent(f) for f in Flow if f is not Flow.AWARENESS)


# ==========================================================================
# 2. Observed behaviour matches the table
# ==========================================================================

@sync
async def test_read_bus_unavailable_matches_fail_open():
    assert decide(Flow.READ, Failure.BUS_UNAVAILABLE) is Disposition.FAIL_OPEN
    bus = FakeBus()

    async def boom(*a, **k):
        raise bus_client.BusUnavailable("timeout")
    bus.search = boom

    out, diag = await flows.handle_read(nr_with("ESDS_SEARCH x\nthe real question"), {}, bus=bus)
    assert diag["injected"] is False
    user = "\n".join(b.get("text", "") for m in out.messages if m.role == "user"
                     for b in m.content if b.get("type") == "text")
    assert "the real question" in user, "fail-open must still forward the turn"


@sync
async def test_read_unknown_identity_matches_fail_closed():
    assert decide(Flow.READ, Failure.UNKNOWN_IDENTITY) is Disposition.FAIL_CLOSED
    bus = FakeBus()
    calls = []

    async def spy(*a, **k):
        calls.append(1)
        return []
    bus.search = spy

    _, diag = await flows.handle_read(
        nr_with("ESDS_SEARCH x", account=UNKNOWN_ACCOUNT), {}, bus=bus)
    assert diag["reason"] == "unknown_account"
    assert calls == [], "fail-closed must not reach the bus at all"


@sync
async def test_approve_bus_unavailable_matches_queue():
    assert decide(Flow.WRITE_APPROVE, Failure.BUS_UNAVAILABLE) is Disposition.QUEUE
    pid, _, _, _ = await submit_and_capture(FakeBus())
    down = FakeBus(raises=bus_client.BusUnavailable("refused"))
    out, diag = await flows.handle_write_request(nr(f"ESDS_APPROVE {pid}"), {}, bus=down)

    assert diag["ingested"] is False
    record = pending.load(pid)
    assert record["status"] == pending.STATUS_QUEUED
    assert record.get("ingest_payload"), "queued write must freeze what was approved"
    text = injected_text(out)
    assert "QUEUED" in text and "not been saved" in text.lower()


@sync
async def test_approve_bus_auth_matches_surface_and_keeps_the_draft():
    assert decide(Flow.WRITE_APPROVE, Failure.BUS_AUTH) is Disposition.SURFACE
    pid, _, _, _ = await submit_and_capture(FakeBus())
    bad = FakeBus(raises=bus_client.BusAuthError("401"))
    out, diag = await flows.handle_write_request(nr(f"ESDS_APPROVE {pid}"), {}, bus=bad)
    assert diag["reason"] == "bus_401"
    assert "FAILED" in injected_text(out)
    assert pending.load(pid) is not None, "the draft must survive for a retry"


@sync
async def test_submit_draft_invalid_matches_retry_bounded():
    assert decide(Flow.WRITE_SUBMIT, Failure.DRAFT_INVALID) is Disposition.RETRY_BOUNDED
    pid, _, rdiag, _ = await submit_and_capture(FakeBus(), draft_text="no draft here")
    assert rdiag["retryable"] is True
    assert flows.MAX_DRAFT_RETRIES <= 2, "retries are billed API calls; keep them bounded"
    assert pending.load(pid) is None, "an invalid draft must not be stored"


# ==========================================================================
# 3. The drain — "queued" must eventually become "saved"
# ==========================================================================

@sync
async def test_queued_write_is_drained_on_the_next_request():
    """Otherwise 'queued' is just a nicer word for 'lost'."""
    pid, _, _, _ = await submit_and_capture(FakeBus(), session="sess-drain")
    down = FakeBus(raises=bus_client.BusUnavailable("refused"))
    await flows.handle_write_request(
        nr(f"ESDS_APPROVE {pid}", session="sess-drain"), {}, bus=down)
    assert pending.load(pid)["status"] == pending.STATUS_QUEUED

    # bus comes back; the next request from this session drains it
    up = FakeBus()
    _, diag = await flows.handle_write_request(
        nr("just a normal turn", session="sess-drain"), {}, bus=up)

    assert len(up.ingest_calls) == 1, "queued write was not retried"
    assert diag.get("drained") and diag["drained"][0]["ok"] is True
    assert pending.load(pid)["status"] == pending.STATUS_APPROVED, "drained write should be marked approved"


@sync
async def test_drain_retries_exactly_what_was_approved():
    pid, _, _, _ = await submit_and_capture(FakeBus(), session="sess-frozen")
    down = FakeBus(raises=bus_client.BusUnavailable("refused"))
    await flows.handle_write_request(
        nr(f"ESDS_APPROVE {pid} --visibility org", session="sess-frozen"), {}, bus=down)

    up = FakeBus()
    await flows.drain_queued_writes("sess-frozen", KNOWN_ACCOUNT, bus=up)
    sent = up.ingest_calls[0]["payload"]
    assert sent["visibility"] == "org", "drain must replay the approved visibility"
    assert up.ingest_calls[0]["idempotency_key"], "drain must stay idempotent"


@sync
async def test_drain_is_idempotent_across_repeated_attempts():
    pid, _, _, _ = await submit_and_capture(FakeBus(), session="sess-idem")
    down = FakeBus(raises=bus_client.BusUnavailable("refused"))
    await flows.handle_write_request(
        nr(f"ESDS_APPROVE {pid}", session="sess-idem"), {}, bus=down)

    up = FakeBus()
    await flows.drain_queued_writes("sess-idem", KNOWN_ACCOUNT, bus=up)
    await flows.drain_queued_writes("sess-idem", KNOWN_ACCOUNT, bus=up)
    assert len(up.ingest_calls) == 1, "a drained write was re-sent"


@sync
async def test_drain_leaves_the_write_queued_if_the_bus_is_still_down():
    pid, _, _, _ = await submit_and_capture(FakeBus(), session="sess-still-down")
    down = FakeBus(raises=bus_client.BusUnavailable("refused"))
    await flows.handle_write_request(
        nr(f"ESDS_APPROVE {pid}", session="sess-still-down"), {}, bus=down)

    results = await flows.drain_queued_writes("sess-still-down", KNOWN_ACCOUNT, bus=down)
    assert results and results[0]["ok"] is False
    assert pending.load(pid)["status"] == pending.STATUS_QUEUED, "must stay queued"


@sync
async def test_drain_never_touches_another_sessions_queue():
    pid, _, _, _ = await submit_and_capture(FakeBus(), session="sess-owner-q")
    down = FakeBus(raises=bus_client.BusUnavailable("refused"))
    await flows.handle_write_request(
        nr(f"ESDS_APPROVE {pid}", session="sess-owner-q"), {}, bus=down)

    up = FakeBus()
    await flows.drain_queued_writes("sess-other", KNOWN_ACCOUNT, bus=up)
    assert up.ingest_calls == []
    assert pending.load(pid)["status"] == pending.STATUS_QUEUED


@sync
async def test_drain_with_unknown_identity_does_nothing():
    pid, _, _, _ = await submit_and_capture(FakeBus(), session="sess-noid")
    down = FakeBus(raises=bus_client.BusUnavailable("refused"))
    await flows.handle_write_request(
        nr(f"ESDS_APPROVE {pid}", session="sess-noid"), {}, bus=down)

    up = FakeBus()
    results = await flows.drain_queued_writes("sess-noid", UNKNOWN_ACCOUNT, bus=up)
    assert results == [] and up.ingest_calls == []


@sync
async def test_drain_can_be_disabled(monkeypatch):
    monkeypatch.setenv("DP_DRAIN_ON_REQUEST", "0")
    pid, _, _, _ = await submit_and_capture(FakeBus(), session="sess-nodrain")
    down = FakeBus(raises=bus_client.BusUnavailable("refused"))
    await flows.handle_write_request(
        nr(f"ESDS_APPROVE {pid}", session="sess-nodrain"), {}, bus=down)

    up = FakeBus()
    await flows.handle_write_request(nr("normal turn", session="sess-nodrain"), {}, bus=up)
    assert up.ingest_calls == []
    assert pending.load(pid)["status"] == pending.STATUS_QUEUED
