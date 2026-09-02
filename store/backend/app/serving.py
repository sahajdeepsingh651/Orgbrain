"""GET /v1/search, /v1/agent-activity, /v1/handoff/{session_id}.

Visibility (private/team/department/org) is enforced here against the caller's
identity (resolved from the bearer token, see app/auth.py) — a caller only ever
receives what its identity is allowed to see (data-passport-core-service.md §5).

Known gap in /v1/search, left as documented and accepted (see decisions-log.md,
2026-08-07): visibility is filtered on the ANN candidate set the HNSW index returns,
not before it. A permitted match can be silently missing if it falls outside the
candidate window. Do not "fix" this with an unindexed exact scan.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.auth import Identity, require_identity
from app.embeddings import get_embedding_model

router = APIRouter()

SSE_KEEPALIVE_SECONDS = 15

ANN_CANDIDATE_WINDOW = 50
JSONB_FIELDS = {
    "sensitivity_flags", "links", "key_points", "next_steps",
    "open_questions", "entities", "artifacts", "domain_data",
}

VISIBILITY_CLAUSE = """(
    visibility = 'org'
    OR (visibility = 'department' AND department = $DEPT)
    OR (visibility = 'team' AND team = $TEAM)
    OR author_user_id = $USER
)"""

# context_bus_events has no author_user_id (schema.md §5's compact projection omits it) —
# see _event_visible's docstring for why that means no "own content" override for bus events.
BUS_VISIBILITY_CLAUSE = """(
    visibility = 'org'
    OR (visibility = 'department' AND department = $DEPT)
    OR (visibility = 'team' AND team = $TEAM)
)"""


def _row_to_dict(row: asyncpg.Record) -> dict:
    out = {}
    for key, value in dict(row).items():
        if key in JSONB_FIELDS and value is not None:
            out[key] = json.loads(value)
        elif isinstance(value, uuid.UUID):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    if "record_id" in out:
        out["gold_ref"] = f"/v1/knowledge/{out['record_id']}"
    return out


async def do_search(
    pool: asyncpg.Pool, identity: Identity, q: str, limit: int = 10,
    department: str | None = None, team: str | None = None,
) -> list[dict]:
    """Shared logic behind GET /v1/search and the search_knowledge MCP tool — one
    implementation, two protocol faces (data-passport-core-service.md §5)."""
    embedding = list(get_embedding_model().embed([q]))[0].tolist()

    rows = await pool.fetch(
        f"""
        WITH ann AS (
            SELECT record_id, embedding <=> $1::vector AS distance
            FROM knowledge_embeddings
            ORDER BY embedding <=> $1::vector
            LIMIT $2
        ),
        keyword AS (
            SELECT record_id
            FROM knowledge_entries
            WHERE title ILIKE '%' || $3 || '%' OR summary ILIKE '%' || $3 || '%'
            LIMIT $2
        ),
        candidates AS (
            SELECT record_id FROM ann
            UNION
            SELECT record_id FROM keyword
        )
        SELECT
            ke.record_id, ke.session_id, ke.title, ke.summary, ke.department, ke.team,
            ke.agent_id, ke.status, ke.visibility, ke.outcome, ke.created_at,
            kem.embedding <=> $1::vector AS distance
        FROM candidates c
        JOIN knowledge_entries ke ON ke.record_id = c.record_id
        JOIN knowledge_embeddings kem ON kem.record_id = c.record_id
        WHERE {VISIBILITY_CLAUSE.replace('$DEPT', '$4').replace('$TEAM', '$5').replace('$USER', '$6')}
        AND ($7::text IS NULL OR ke.department = $7)
        AND ($8::text IS NULL OR ke.team = $8)
        ORDER BY distance ASC
        LIMIT $9
        """,
        str(embedding), ANN_CANDIDATE_WINDOW, q,
        identity.department, identity.team, identity.user_id,
        department, team, limit,
    )
    return [_row_to_dict(r) for r in rows]


@router.get("/v1/search")
async def search(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
    team: str | None = None,
    identity: Identity = Depends(require_identity),
):
    pool: asyncpg.Pool = request.app.state.db_pool
    results = await do_search(pool, identity, q, limit, department, team)
    return {"results": results}


async def do_agent_activity(pool: asyncpg.Pool, identity: Identity, team: str | None = None) -> list[dict]:
    """Shared logic behind GET /v1/agent-activity and the get_agent_activity MCP tool."""
    rows = await pool.fetch(
        f"""
        WITH latest AS (
            SELECT DISTINCT ON (agent_id) *
            FROM knowledge_entries
            WHERE agent_id IS NOT NULL
            ORDER BY agent_id, created_at DESC
        )
        SELECT record_id, agent_id, author_user_id, department, team, session_id,
               title, status, outcome, visibility, created_at
        FROM latest
        WHERE {VISIBILITY_CLAUSE.replace('$DEPT', '$1').replace('$TEAM', '$2').replace('$USER', '$3')}
        AND ($4::text IS NULL OR team = $4)
        ORDER BY created_at DESC
        """,
        identity.department, identity.team, identity.user_id, team,
    )
    return [_row_to_dict(r) for r in rows]


@router.get("/v1/agent-activity")
async def agent_activity(
    request: Request,
    team: str | None = None,
    identity: Identity = Depends(require_identity),
):
    pool: asyncpg.Pool = request.app.state.db_pool
    results = await do_agent_activity(pool, identity, team)
    return {"results": results}


async def do_handoff(pool: asyncpg.Pool, identity: Identity, session_id: str) -> dict:
    """Shared logic behind GET /v1/handoff/{session_id} and the handoff MCP tool.
    Raises LookupError if the session doesn't exist or isn't visible to this identity —
    both collapse to the same outcome, so a caller can't distinguish "doesn't exist"
    from "exists but you can't see it"."""
    row = await pool.fetchrow(
        f"""
        SELECT * FROM knowledge_entries
        WHERE session_id = $1
        AND {VISIBILITY_CLAUSE.replace('$DEPT', '$2').replace('$TEAM', '$3').replace('$USER', '$4')}
        ORDER BY created_at DESC
        LIMIT 1
        """,
        session_id, identity.department, identity.team, identity.user_id,
    )
    if row is None:
        raise LookupError(f"session '{session_id}' not found")
    return _row_to_dict(row)


@router.get("/v1/handoff/{session_id}")
async def handoff(
    request: Request,
    session_id: str,
    identity: Identity = Depends(require_identity),
):
    pool: asyncpg.Pool = request.app.state.db_pool
    try:
        return await do_handoff(pool, identity, session_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="session not found")


async def do_get_knowledge(pool: asyncpg.Pool, identity: Identity, record_id: str) -> dict:
    """S1 — shared logic behind GET /v1/knowledge/{record_id} and the (future)
    get_knowledge MCP tool. Same as do_handoff: not-found and not-visible
    collapse to 404, so a caller can't distinguish 'doesn't exist' from
    'exists but you can't see it'. Reuses VISIBILITY_CLAUSE verbatim.

    `gold_ref` is minted in main.py:139 and serving.py:_row_to_dict:62 (every
    search result and every bus event carries one). Until S1, all of those
    linked to a 404 — now they resolve to the full record the caller is
    allowed to see."""
    try:
        rid = uuid.UUID(record_id)
    except (ValueError, TypeError):
        raise LookupError(f"record_id '{record_id}' is not a valid UUID")
    row = await pool.fetchrow(
        f"""
        SELECT * FROM knowledge_entries
        WHERE record_id = $1
        AND {VISIBILITY_CLAUSE.replace('$DEPT', '$2').replace('$TEAM', '$3').replace('$USER', '$4')}
        """,
        rid, identity.department, identity.team, identity.user_id,
    )
    if row is None:
        raise LookupError(f"record '{record_id}' not found")
    return _row_to_dict(row)


@router.get("/v1/knowledge/{record_id}")
async def get_knowledge(
    request: Request,
    record_id: str,
    identity: Identity = Depends(require_identity),
):
    pool: asyncpg.Pool = request.app.state.db_pool
    try:
        return await do_get_knowledge(pool, identity, record_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="knowledge record not found")


def _event_visible(event: dict, identity: Identity) -> bool:
    """Mirrors BUS_VISIBILITY_CLAUSE, but in Python — live NOTIFY payloads arrive
    unfiltered (Postgres broadcasts to every listener on the channel), so each one
    is checked against the caller's identity here before it's ever sent to them.

    No "own content" override here: schema.md §5's compact bus projection has no
    author_user_id field (deliberately kept small), so a 'private' record's bus event
    can't be attributed back to its author from the payload alone — it's simply never
    delivered to anyone via the bus, author included. REST (/v1/search, /v1/handoff)
    still grants the author access, since those query knowledge_entries directly,
    which does have author_user_id."""
    visibility = event.get("visibility")
    return (
        visibility == "org"
        or (visibility == "department" and event.get("department") == identity.department)
        or (visibility == "team" and event.get("team") == identity.team)
    )


def _matches_narrowing(event: dict, department: str | None, team: str | None, visibility: str | None) -> bool:
    return (
        (department is None or event.get("department") == department)
        and (team is None or event.get("team") == team)
        and (visibility is None or event.get("visibility") == visibility)
    )


def _sse_format(event: dict) -> str:
    event_id = event.get("timestamp", "")
    return f"id: {event_id}\nevent: {event.get('event_type', 'context_bus')}\ndata: {json.dumps(event)}\n\n"


@router.get("/v1/bus/subscribe")
async def bus_subscribe(
    request: Request,
    department: str | None = None,
    team: str | None = None,
    visibility: str | None = None,
    since: str | None = None,
    identity: Identity = Depends(require_identity),
):
    pool: asyncpg.Pool = request.app.state.db_pool
    last_event_id = request.headers.get("last-event-id") or since
    catchup_since = datetime.fromisoformat(last_event_id) if last_event_id else datetime.now(timezone.utc)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        def on_notify(connection, pid, channel, payload):
            queue.put_nowait(json.loads(payload))

        async with pool.acquire() as conn:
            catchup_rows = await conn.fetch(
                f"""
                SELECT record_id, session_id, department, team, agent_id, event_type,
                       title, summary, status, visibility, gold_ref, created_at
                FROM context_bus_events
                WHERE created_at > $1
                AND {BUS_VISIBILITY_CLAUSE.replace('$DEPT', '$2').replace('$TEAM', '$3')}
                ORDER BY created_at
                """,
                catchup_since, identity.department, identity.team,
            )
            for row in catchup_rows:
                event = _row_to_dict(row)
                event["timestamp"] = event.pop("created_at")
                if _matches_narrowing(event, department, team, visibility):
                    yield _sse_format(event)

            await conn.add_listener("context_bus", on_notify)
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if _event_visible(event, identity) and _matches_narrowing(event, department, team, visibility):
                        yield _sse_format(event)
            finally:
                await conn.remove_listener("context_bus", on_notify)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
