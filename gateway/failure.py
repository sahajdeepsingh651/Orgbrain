"""G8 — failure semantics as data, not prose.

Every flow in this gateway already behaves correctly when something breaks.
The problem this module solves is that those decisions lived in three
different files as inline `except` branches, so "what happens when the bus
is down during a write?" could only be answered by reading code — and two
flows could drift apart without anything noticing.

Here the policy is a TABLE. `POLICY` is exhaustive over Flow x Failure (a
test asserts that), so adding a flow or a failure mode forces an explicit
decision rather than inheriting whatever the nearest `except` happened to do.

The two rules that must never be weakened:

  * DLP failures FAIL CLOSED. Everything else may fail open; a redaction
    that could not be performed may not be skipped.
  * A write that did not happen is NEVER reported as success. Queueing is
    honest ("it is queued"); claiming a save is the one lie this system
    cannot afford, because the whole product is "the human decides what
    enters organisational memory".
"""
from __future__ import annotations

from enum import Enum


class Flow(str, Enum):
    NORMAL = "normal"                 # plain passthrough, no bus involvement
    READ = "read"                     # ESDS_SEARCH
    AWARENESS = "awareness"           # the unprompted probe
    WRITE_SUBMIT = "write_submit"     # ESDS_SUBMIT -> draft -> pending
    WRITE_APPROVE = "write_approve"   # ESDS_APPROVE -> POST /v1/ingest


class Failure(str, Enum):
    BUS_UNAVAILABLE = "bus_unavailable"    # timeout / refused / 5xx
    BUS_AUTH = "bus_auth"                  # 401 from the bus
    BUS_SCHEMA = "bus_schema"              # 422 from /v1/ingest
    UNKNOWN_IDENTITY = "unknown_identity"  # account_uuid not in the map
    DLP = "dlp"                            # a detector itself failed
    DRAFT_INVALID = "draft_invalid"        # the model's draft did not validate


class Disposition(str, Enum):
    FAIL_OPEN = "fail_open"          # forward unchanged, log, carry on
    FAIL_CLOSED = "fail_closed"      # refuse the operation
    QUEUE = "queue"                  # persist, retry later, tell the truth
    RETRY_BOUNDED = "retry_bounded"  # bounded side-call retries, then give up
    SURFACE = "surface"              # terminal; tell the human plainly


# (flow, failure) -> disposition. Exhaustive by construction; see
# test_g8_failures.py::test_policy_is_exhaustive.
POLICY: dict[tuple[Flow, Failure], Disposition] = {
    # NORMAL never touches the bus, so bus failures cannot affect it.
    (Flow.NORMAL, Failure.BUS_UNAVAILABLE): Disposition.FAIL_OPEN,
    (Flow.NORMAL, Failure.BUS_AUTH): Disposition.FAIL_OPEN,
    (Flow.NORMAL, Failure.BUS_SCHEMA): Disposition.FAIL_OPEN,
    (Flow.NORMAL, Failure.UNKNOWN_IDENTITY): Disposition.FAIL_OPEN,
    (Flow.NORMAL, Failure.DLP): Disposition.FAIL_CLOSED,
    (Flow.NORMAL, Failure.DRAFT_INVALID): Disposition.FAIL_OPEN,

    # READ: a developer must never be blocked from working because the
    # knowledge base is down. Losing context degrades the answer; losing
    # the session stops the work.
    (Flow.READ, Failure.BUS_UNAVAILABLE): Disposition.FAIL_OPEN,
    # 401 also fails open (the turn still goes upstream) but is SURFACE-d
    # in the log, because a misconfigured token must not be
    # indistinguishable from "nothing relevant exists".
    (Flow.READ, Failure.BUS_AUTH): Disposition.SURFACE,
    (Flow.READ, Failure.BUS_SCHEMA): Disposition.FAIL_OPEN,
    # No identity means visibility cannot be enforced. Guessing a default
    # token would hand one user another user's records.
    (Flow.READ, Failure.UNKNOWN_IDENTITY): Disposition.FAIL_CLOSED,
    (Flow.READ, Failure.DLP): Disposition.FAIL_CLOSED,
    (Flow.READ, Failure.DRAFT_INVALID): Disposition.FAIL_OPEN,

    # AWARENESS is unprompted, so every failure is silent. A probe that
    # reports its own errors is worse than one that quietly doesn't fire.
    (Flow.AWARENESS, Failure.BUS_UNAVAILABLE): Disposition.FAIL_OPEN,
    (Flow.AWARENESS, Failure.BUS_AUTH): Disposition.FAIL_OPEN,
    (Flow.AWARENESS, Failure.BUS_SCHEMA): Disposition.FAIL_OPEN,
    (Flow.AWARENESS, Failure.UNKNOWN_IDENTITY): Disposition.FAIL_OPEN,
    (Flow.AWARENESS, Failure.DLP): Disposition.FAIL_CLOSED,
    (Flow.AWARENESS, Failure.DRAFT_INVALID): Disposition.FAIL_OPEN,

    # WRITE_SUBMIT touches no bus endpoint — it only parks a draft. An
    # unknown identity still fails closed, because a draft that could never
    # be ingested should not be produced at all.
    (Flow.WRITE_SUBMIT, Failure.BUS_UNAVAILABLE): Disposition.FAIL_OPEN,
    (Flow.WRITE_SUBMIT, Failure.BUS_AUTH): Disposition.FAIL_OPEN,
    (Flow.WRITE_SUBMIT, Failure.BUS_SCHEMA): Disposition.FAIL_OPEN,
    (Flow.WRITE_SUBMIT, Failure.UNKNOWN_IDENTITY): Disposition.FAIL_CLOSED,
    (Flow.WRITE_SUBMIT, Failure.DLP): Disposition.FAIL_CLOSED,
    # The model produced no draft / a malformed one: bounded side-call
    # retries. Never in the user's thread — correction chatter appended to
    # the conversation becomes cache prefix for every subsequent turn.
    (Flow.WRITE_SUBMIT, Failure.DRAFT_INVALID): Disposition.RETRY_BOUNDED,

    # WRITE_APPROVE is the only flow that mutates organisational memory.
    (Flow.WRITE_APPROVE, Failure.BUS_UNAVAILABLE): Disposition.QUEUE,
    (Flow.WRITE_APPROVE, Failure.BUS_AUTH): Disposition.SURFACE,
    # 422 here is NOT retried through the model: by this point a human has
    # already approved specific content, and silently re-drafting it would
    # store something they never saw.
    (Flow.WRITE_APPROVE, Failure.BUS_SCHEMA): Disposition.SURFACE,
    (Flow.WRITE_APPROVE, Failure.UNKNOWN_IDENTITY): Disposition.FAIL_CLOSED,
    (Flow.WRITE_APPROVE, Failure.DLP): Disposition.FAIL_CLOSED,
    (Flow.WRITE_APPROVE, Failure.DRAFT_INVALID): Disposition.FAIL_CLOSED,
}


def decide(flow: Flow, failure: Failure) -> Disposition:
    """Look up the policy. KeyError is deliberate — an unlisted combination
    is a missing decision, not a case to default."""
    return POLICY[(flow, failure)]


def reports_success(disposition: Disposition) -> bool:
    """Whether the human may be told the operation succeeded. Only ever
    false here — this exists so the rule is greppable and testable rather
    than an unwritten convention."""
    return False


def is_silent(flow: Flow) -> bool:
    """AWARENESS never surfaces its own failures to the developer."""
    return flow is Flow.AWARENESS
