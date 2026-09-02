"""G4 — the Interceptor's REST client for the Context Bus.

The gateway is a REST client of the bus, deliberately NOT an MCP client:
`store/backend/mcp_server.py` runs over stdio and resolves ONE identity per
process at startup (MCP_API_TOKEN), which is the wrong shape for a shared
proxy serving many developers on one process. REST carries a per-call
bearer token, which is exactly what identity.resolve() hands us.

Invariant this module serves: all AI-session-originated traffic to the
Context Bus passes through the Interceptor. This is the only place the
gateway is allowed to speak to the bus.

Failure contract — the caller decides, not this module:
    BusUnavailable  timeout / connection refused / 5xx. READ and AWARENESS
                    fail OPEN (forward the request unchanged); WRITE queues.
    BusAuthError    401. Terminal — surface it; never silently downgrade to
                    "no results", which would look identical to "nothing
                    relevant exists" and hide a misconfigured token.
Nothing here fails open on its own; a policy that swallows an exception is
making that choice explicitly and visibly.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

BASE_URL = os.environ.get("DP_BUS_BASE_URL", "http://127.0.0.1:8000")

# Retrieval sits in the request path of a human's turn, so a slow bus must
# degrade rather than stall the developer's session. Awareness overrides
# this with something much tighter (see read policy).
DEFAULT_TIMEOUT = float(os.environ.get("DP_BUS_TIMEOUT", "3.0"))
# Ingest is not in a latency-sensitive path and is the one call whose loss
# actually costs data, so it gets longer.
INGEST_TIMEOUT = float(os.environ.get("DP_BUS_INGEST_TIMEOUT", "10.0"))


class BusError(Exception):
    """Base for every bus failure."""


class BusUnavailable(BusError):
    """Timeout, connection failure, or 5xx — the bus may be fine later."""


class BusAuthError(BusError):
    """401 from the bus. The token is wrong; retrying will not help."""


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE_URL)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise BusAuthError("bus rejected the bearer token (401)")
    if resp.status_code >= 500:
        raise BusUnavailable(f"bus returned {resp.status_code}")


async def search(
    query: str,
    *,
    token: str,
    limit: int = 10,
    department: str | None = None,
    team: str | None = None,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """GET /v1/search. The bus does the embedding and enforces visibility
    against the token's identity — the gateway never sees a record the
    caller isn't allowed to see, and never needs fastembed of its own.

    Returns the `results` list (possibly empty). Empty is a legitimate
    answer, not an error: injecting nothing is correct when nothing is
    relevant.
    """
    params: dict[str, Any] = {"q": query, "limit": limit}
    if department:
        params["department"] = department
    if team:
        params["team"] = team
    try:
        resp = await _get_client().get(
            "/v1/search", params=params, headers=_auth(token),
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise BusUnavailable(f"search: {type(exc).__name__}") from exc
    _raise_for_status(resp)
    if resp.status_code >= 400:
        return []
    return (resp.json() or {}).get("results", [])


async def get_knowledge(record_id: str, *, token: str,
                        timeout: float | None = None) -> dict[str, Any] | None:
    """GET /v1/knowledge/{record_id} (store-side S1). Returns None on 404 —
    which the bus also returns for 'exists but not visible to you', so a
    caller cannot distinguish the two. That collapse is deliberate on the
    store side; don't try to undo it here."""
    try:
        resp = await _get_client().get(
            f"/v1/knowledge/{record_id}", headers=_auth(token),
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise BusUnavailable(f"get_knowledge: {type(exc).__name__}") from exc
    _raise_for_status(resp)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        return None
    return resp.json()


async def ingest(payload: dict, *, token: str, idempotency_key: str | None = None,
                 timeout: float | None = None) -> tuple[int, dict]:
    """POST /v1/ingest. Returns (status_code, body) rather than raising on
    422, because the WRITE policy treats a schema rejection as a RETRYABLE
    outcome (bounded, via a side call) and needs the body's {field, reason}
    to tell the model what to fix. 401 and 5xx still raise — those are
    terminal and queue-worthy respectively, not things to hand back to the
    model.

    `idempotency_key` should be sha256(session_id + canonical draft JSON):
    a retry after a timeout then returns 200 "deduplicated" naming the
    original record_id instead of writing a second passport.
    """
    headers = _auth(token)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        resp = await _get_client().post(
            "/v1/ingest", json=payload, headers=headers,
            timeout=timeout if timeout is not None else INGEST_TIMEOUT,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise BusUnavailable(f"ingest: {type(exc).__name__}") from exc
    _raise_for_status(resp)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}
