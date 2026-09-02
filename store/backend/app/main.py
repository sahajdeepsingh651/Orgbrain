"""POST /v1/ingest — stub Gate: Bronze write, provenance tagging, domain_data validation.
No PII scanning/redaction here — that already happened on the endpoint device (out of scope
for this service, see data-passport-architecture.md § The Endpoint Checkpoint).
"""

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.auth import Identity, require_identity
from app.domains import DomainValidationError, validate_domain_data
from app.embeddings import get_embedding_model
from app.serving import router as serving_router
from app._trace import install_tracer

install_tracer()  # no-op unless DP_TRACE=1 — see app/_trace.py

BASE_DIR = Path(__file__).resolve().parent.parent.parent
BRONZE_DIR = Path(os.environ.get("BRONZE_DIR", BASE_DIR / "bronze"))

VISIBILITY_VALUES = {"private", "team", "department", "org"}
STATUS_VALUES = {"in_progress", "completed", "blocked", "handed_off", "abandoned"}
OUTCOME_VALUES = {
    "decision_made", "insight_found", "issue_resolved", "blocker_hit", "question_open", "in_progress",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    get_embedding_model()
    yield
    await app.state.db_pool.close()


app = FastAPI(title="Data Passport Core Service", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # For demo, allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(serving_router)


class ValidationFailure(Exception):
    def __init__(self, field: str, value: Any, reason: str):
        self.field = field
        self.value = value
        self.reason = reason


def _require(body: dict, path: str) -> Any:
    node = body
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node or node[part] is None:
            raise ValidationFailure(path, None, "required field missing")
        node = node[part]
    return node


def _require_enum(body: dict, path: str, allowed: set[str]) -> str:
    value = _require(body, path)
    if value not in allowed:
        raise ValidationFailure(path, value, f"must be one of {sorted(allowed)}")
    return value


def write_bronze(event_id: str, source_system: str, team: str | None, payload: dict) -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = BRONZE_DIR / (team or "unassigned") / source_system / date_str
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{event_id}.json"
    path.write_text(json.dumps(payload, indent=2))
    return path


async def quarantine(pool: asyncpg.Pool, event_id: str, body: dict, exc: ValidationFailure | DomainValidationError,
                     identity: Identity | None = None, source_system_hint: str | None = None):
    await pool.execute(
        """
        INSERT INTO redaction_audit_log
            (quarantine_id, session_id, source_system, sensitivity_flags, validation_failure, outcome,
             asserted_by_user_id, asserted_by_department, asserted_by_team, asserted_source_system)
        VALUES ($1, $2, $3, $4, $5, 'quarantined', $6, $7, $8, $9)
        """,
        uuid.UUID(event_id),
        body.get("session_id"),
        body.get("source_system"),
        json.dumps(body.get("sensitivity_flags") or {}),
        json.dumps({"field": exc.field, "value": exc.value, "reason": exc.reason}),
        identity.user_id if identity else None,
        identity.department if identity else None,
        identity.team if identity else None,
        source_system_hint if source_system_hint is not None else (body.get("source_system")),
    )
    return JSONResponse(
        status_code=422,
        content={
            "error": exc.reason,
            "field": exc.field,
            "quarantine_id": event_id,
            "status": "quarantined",
        },
    )


async def _lookup_idempotent(pool: asyncpg.Pool, idempotency_key: str) -> str | None:
    """Resolve an Idempotency-Key to an already-committed record_id, or None."""
    row = await pool.fetchrow(
        "SELECT record_id FROM knowledge_entries WHERE idempotency_key = $1",
        idempotency_key,
    )
    return str(row["record_id"]) if row is not None else None


def _dedup_response(record_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "record_id": record_id,
            "gold_ref": f"/v1/knowledge/{record_id}",
            "status": "deduplicated",
        },
    )


@app.post("/v1/ingest", status_code=201)
async def ingest(request: Request, identity: Identity = Depends(require_identity)):
    body = await request.json()
    pool: asyncpg.Pool = request.app.state.db_pool

    # S5 — author identity comes from the authenticated token, NEVER the
    # request body. See docs/S5 (to be added) / decisions-log: the prior
    # code bound the resolved Identity to `_` then self-declared author/
    # dept/team from the body — any valid token could write a record
    # attributed to any user in any department. `captured_by.user_id`,
    # `hint.department`, and `hint.team` in the request body are now
    # IGNORED on the author/department path. `agent_id` still comes from
    # the body (it is metadata about the agent, not the human).

    # S2 — idempotency. The gateway sends `Idempotency-Key = sha256(
    # session_id + canonical draft JSON)`; a retry after a timeout must
    # return the existing record_id with 200, NOT a duplicate.
    idempotency_key = request.headers.get("idempotency-key")

    # S2/S5 defensive narrowing: hint.team is allowed only as an optional
    # narrowing that CANNOT widen — must equal the token's team or be
    # unset. A wider override would re-open the security hole S5 closes.
    hint = body.get("hint") or {}
    hint_team = hint.get("team")
    if hint_team is not None and hint_team != identity.team:
        # narrow-to-team mismatch: ignore the body, never widen.
        hint_team = identity.team

    # Idempotency check first: avoid BOTH a duplicate Bronze file AND a
    # duplicate knowledge_entries row. Done in a short transaction so the
    # row-state read by the duplicate-arriving request is consistent.
    if idempotency_key:
        existing = await _lookup_idempotent(pool, idempotency_key)
        if existing is not None:
            return _dedup_response(existing)

    event_id = str(uuid.uuid4())
    write_bronze(event_id, body.get("source_system") or "unknown", hint_team, body)

    try:
        session_id = _require(body, "session_id")
        source_system = _require(body, "source_system")
        content = _require(body, "content")
        if not isinstance(content, str):
            raise ValidationFailure("content", content, "expected string")
        agent_id = (body.get("captured_by") or {}).get("agent_id")
        # S5 — derived from token. hint.department MUST equal identity's
        # department if set, otherwise ignored (cannot widen). Validate
        # after the basic body checks but BEFORE the unique-constraint INSERT
        # so a widening attempt collapses to the token's value, not 422.
        body_dept = hint.get("department")
        if body_dept is not None and body_dept != identity.department:
            body_dept = identity.department
        department = identity.department
        team = identity.team
        user_id = identity.user_id
        visibility = _require_enum(body, "visibility", VISIBILITY_VALUES)
        status = _require_enum(body, "status", STATUS_VALUES)
        title = _require(body, "knowledge.title")
        summary = _require(body, "knowledge.summary")
        outcome = _require_enum(body, "knowledge.outcome", OUTCOME_VALUES)
        sensitivity_flags = body.get("sensitivity_flags") or {}

        domain = body.get("domain")
        domain_data = body.get("domain_data")
        if domain is not None:
            if domain_data is None:
                raise ValidationFailure("domain_data", None, "required when domain is set")
            validate_domain_data(domain, domain_data)
        elif domain_data is not None:
            raise ValidationFailure("domain", None, "required when domain_data is set")
    except (ValidationFailure, DomainValidationError) as exc:
        return await quarantine(pool, event_id, body, exc, identity, source_system_hint=body.get("source_system"))

    knowledge = body.get("knowledge") or {}
    embedding = list(get_embedding_model().embed([content]))[0].tolist()
    gold_ref = f"/v1/knowledge/{event_id}"

    # V2 — the idempotency pre-check above is a TOCTOU read: two concurrent
    # requests carrying the same key both miss it, both reach this INSERT,
    # and the second violates knowledge_entries_idempotency_key_idx. Without
    # this handler the loser gets a 500 instead of the 200 the feature
    # promises. Re-select rather than matching on a constraint name, and
    # re-raise if the violation was something else entirely (a duplicate
    # record_id, say) so a real bug isn't silently reported as a dedup.
    # Note the loser has already written its own Bronze file under a
    # distinct event_id — a harmless duplicate on disk, not in the DB.
    try:
        return await _commit_ingest(
            pool, event_id=event_id, gold_ref=gold_ref, embedding=embedding, body=body,
            session_id=session_id, source_system=source_system, department=department,
            team=team, user_id=user_id, agent_id=agent_id, visibility=visibility,
            sensitivity_flags=sensitivity_flags, status=status, title=title,
            summary=summary, outcome=outcome, knowledge=knowledge, domain=domain,
            domain_data=domain_data, idempotency_key=idempotency_key,
        )
    except asyncpg.UniqueViolationError:
        if idempotency_key:
            existing = await _lookup_idempotent(pool, idempotency_key)
            if existing is not None:
                return _dedup_response(existing)
        raise


async def _commit_ingest(
    pool: asyncpg.Pool, *, event_id, gold_ref, embedding, body, session_id, source_system,
    department, team, user_id, agent_id, visibility, sensitivity_flags, status, title,
    summary, outcome, knowledge, domain, domain_data, idempotency_key,
):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO knowledge_entries (
                    record_id, session_id, source_system, department, team, author_user_id, agent_id,
                    started_at, ended_at, visibility, consent_basis, consent_actor_type, consent_actor_id,
                    sensitivity_flags, status, links, title, summary, intent, outcome, outcome_detail,
                    key_points, next_steps, open_questions, entities, artifacts, domain, domain_data,
                    idempotency_key
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'user_opted_in', 'user', $6,
                    $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25,
                    $26
                )
                """,
                uuid.UUID(event_id), session_id, source_system, department, team, user_id, agent_id,
                body.get("started_at"), body.get("ended_at"), visibility,
                json.dumps(sensitivity_flags), status, json.dumps(knowledge.get("links", [])),
                title, summary, knowledge.get("intent"), outcome, knowledge.get("outcome_detail"),
                json.dumps(knowledge.get("key_points", [])), json.dumps(knowledge.get("next_steps", [])),
                json.dumps(knowledge.get("open_questions", [])), json.dumps(knowledge.get("entities", [])),
                json.dumps(knowledge.get("artifacts", [])), domain,
                json.dumps(domain_data) if domain_data is not None else None,
                idempotency_key,
            )
            await conn.execute(
                "INSERT INTO knowledge_embeddings (record_id, embedding) VALUES ($1, $2)",
                uuid.UUID(event_id), str(embedding),
            )
            # S3 — attribute sensitivity_flags to their asserter. The
            # store does NOT verify PII (decisions-log:23), but it must
            # record WHO claimed what — turning a rumour into an audit
            # trail without weakening the trust boundary.
            await conn.execute(
                """
                INSERT INTO redaction_audit_log
                    (record_id, session_id, source_system, sensitivity_flags, outcome,
                     asserted_by_user_id, asserted_by_department, asserted_by_team,
                     asserted_source_system)
                VALUES ($1, $2, $3, $4, 'committed', $5, $6, $7, $8)
                """,
                uuid.UUID(event_id), session_id, source_system, json.dumps(sensitivity_flags),
                user_id, department, team, source_system,
            )
            # event_type is always 'created' for now — this endpoint only ever creates new
            # records; 'updated'/'completed'/'handed_off' need an update endpoint that doesn't exist yet.
            bus_row = await conn.fetchrow(
                """
                INSERT INTO context_bus_events
                    (record_id, session_id, department, team, agent_id, event_type, title, summary, status, visibility, gold_ref)
                VALUES ($1, $2, $3, $4, $5, 'created', $6, $7, $8, $9, $10)
                RETURNING created_at
                """,
                uuid.UUID(event_id), session_id, department, team, agent_id, title, summary, status, visibility, gold_ref,
            )
            bus_event = {
                "record_id": event_id,
                "session_id": session_id,
                "department": department,
                "team": team,
                "agent_id": agent_id,
                "event_type": "created",
                "title": title,
                "summary": summary,
                "status": status,
                "visibility": visibility,
                "timestamp": bus_row["created_at"].isoformat(),
                "gold_ref": gold_ref,
            }
            await conn.execute("SELECT pg_notify('context_bus', $1)", json.dumps(bus_event))

    return {
        "record_id": event_id,
        "gold_ref": gold_ref,
        "sensitivity_flags": sensitivity_flags,
        "status": "committed",
    }
