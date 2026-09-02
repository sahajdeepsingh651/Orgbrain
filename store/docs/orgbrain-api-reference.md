# Orgbrain — Core Service API Reference

> Concrete request/response examples for everything built as of 2026-08-08 (all 6 steps of `orgbrain-core-service.md` §7). That doc is the *design contract* with implementation notes woven in; this doc is the practical "how do I actually call this" reference, kept in sync with it. See `orgbrain-setup.md` to get a server running first.

All endpoints require `Authorization: Bearer <token>`, where `<token>` is a key in `config/api_tokens.json` (see `orgbrain-setup.md` §4 for the dev tokens this repo's tests use). A missing/unknown token gets `401 {"detail": "invalid or missing bearer token"}` from every endpoint below, including the SSE one.

Every endpoint's caller identity resolves to `{user_id, department, team}` (`backend/app/auth.py`). Where "visibility" is mentioned below, the rule is always the same one, defined once in `backend/app/serving.py`'s `VISIBILITY_CLAUSE`:

> A record is visible if `visibility = 'org'`, OR `visibility = 'department'` and the record's `department` matches the caller's, OR `visibility = 'team'` and the record's `team` matches the caller's, OR the caller is the record's `author_user_id` (own content is always visible, regardless of its visibility level). **Exception:** the SSE bus (`/v1/bus/subscribe`) has no "own content" rule — see that section.

---

## `POST /v1/ingest`

Writes one record end-to-end: Bronze (raw JSON file) → stub Gate (validation) → `knowledge_entries` + `knowledge_embeddings` + `redaction_audit_log` → `context_bus_events` + `NOTIFY`, all in one request, all inside one Postgres transaction (except the Bronze write, which happens first and unconditionally, before validation).

**Request:**

```bash
curl -X POST http://127.0.0.1:8000/v1/ingest \
  -H "Authorization: Bearer dev-local-token" \
  -H "Content-Type: application/json" \
  -d '{
    "source_system": "claude-code",
    "captured_by": { "user_id": "u-dev", "agent_id": "agent-100" },
    "session_id": "sess-example-1",
    "content": "Fixed a race condition in the token refresh path by adding a mutex.",
    "sensitivity_flags": {
      "contains_pii": false, "contains_credentials": false,
      "redaction_applied": false, "redaction_count": 0
    },
    "visibility": "team",
    "status": "completed",
    "knowledge": {
      "title": "Fixed token refresh race condition",
      "summary": "Root-caused and fixed a race condition in the auth token refresh path.",
      "outcome": "issue_resolved",
      "key_points": ["Race condition traced to concurrent refresh calls", "Added a mutex"],
      "next_steps": ["Add a regression test"]
    },
    "hint": { "department": "Engineering", "team": "platform" },
    "domain": "engineering.v1",
    "domain_data": {
      "repo": "org/auth-service", "files_changed": ["src/auth/refresh.ts"],
      "pr_link": "https://github.com/org/auth-service/pull/42",
      "root_cause": "concurrent refresh calls", "fix_type": "bugfix"
    }
  }'
```

Required fields: `source_system`, `captured_by.user_id`, `session_id`, `content` (string), `visibility` (`private`\|`team`\|`department`\|`org`), `status` (`in_progress`\|`completed`\|`blocked`\|`handed_off`\|`abandoned`), `hint.department`, `knowledge.title`, `knowledge.summary`, `knowledge.outcome` (`decision_made`\|`insight_found`\|`issue_resolved`\|`blocker_hit`\|`question_open`\|`in_progress`). Optional: `captured_by.agent_id`, `hint.team`, `started_at`/`ended_at`, `knowledge.intent`/`outcome_detail`/`key_points`/`next_steps`/`open_questions`/`entities`/`artifacts`/`links` (all default `[]`), `domain`+`domain_data` (both required together or both omitted — see `schemas/domains/*.json`).

**Success — `201`:**

```json
{
  "record_id": "582214d3-45bf-4da7-8d16-2c246c4a3865",
  "gold_ref": "/v1/knowledge/582214d3-45bf-4da7-8d16-2c246c4a3865",
  "sensitivity_flags": { "contains_pii": false, "contains_credentials": false, "redaction_applied": false, "redaction_count": 0 },
  "status": "committed"
}
```

Note: `gold_ref` resolves — `GET /v1/knowledge/{record_id}` was added 2026-08-09 and applies the same visibility rule as `/v1/handoff`, collapsing not-found and not-visible into the same `404`.

**Validation failure — `422`** (missing/malformed required field, bad enum value, or `domain_data` failing its declared type in `schemas/domains/{domain}.json`):

```json
{
  "error": "must be one of ['private', 'team', 'department', 'org']",
  "field": "visibility",
  "quarantine_id": "e41ba47d-283c-452e-ac46-9635d543934c",
  "status": "quarantined"
}
```

The raw payload is always written to Bronze first (`bronze/{team|unassigned}/{source_system}/{yyyy-mm-dd}/{id}.json`) regardless of outcome, and a `redaction_audit_log` row is written with `outcome = 'quarantined'` and the same `{field, value, reason}` in `validation_failure`. Never a PII-related rejection — this endpoint doesn't scan for that (see `orgbrain-core-service.md` §1).

**Auth failure — `401`:** no Bronze write happens at all (auth runs before anything else).

---

## `GET /v1/search`

Hybrid semantic + keyword search. Query params: `q` (required, non-empty), `limit` (1–50, default 10), `department`/`team` (optional — narrow further within what the caller can already see; never widen it).

```bash
curl -G http://127.0.0.1:8000/v1/search \
  -H "Authorization: Bearer dev-local-token" \
  --data-urlencode "q=push notifications" \
  --data-urlencode "limit=10"
```

**Response — `200`:**

```json
{
  "results": [
    {
      "record_id": "...", "session_id": "sess-vis-B", "title": "Team-visible mobile record about push notifications",
      "summary": "team mobile content", "department": "Engineering", "team": "mobile", "agent_id": "agent-B",
      "status": "completed", "visibility": "team", "outcome": "insight_found",
      "created_at": "2026-08-08T10:45:02.1+00:00", "distance": 0.1495,
      "gold_ref": "/v1/knowledge/..."
    }
  ]
}
```

`distance` is cosine distance (lower = more similar), from `embedding <=> query_embedding`. Results are ranked by distance across the union of an HNSW ANN candidate set (top 50) and a plain `ILIKE` keyword match on `title`/`summary` — see the **known, accepted gap** below before assuming a missing result is a bug.

> **HNSW/visibility ordering gap (do not "fix"):** visibility/department/team filtering happens on the ~50-row ANN candidate set the ivfflat/HNSW index returns, not before it. A record the caller is genuinely allowed to see can be silently absent from results if it doesn't make it into that candidate window. This is a documented, accepted trade-off (`decisions-log.md`, 2026-08-07) with a known post-hackathon fix (`pgvector` ≥0.8.0 `hnsw.iterative_scan`) — do not switch to an unindexed exact scan to make it go away.

---

## `GET /v1/agent-activity`

**Derived**, not a separately-maintained ledger: each agent's single most recent `knowledge_entries` row (`DISTINCT ON (agent_id) ... ORDER BY created_at DESC`), visibility-filtered. There is no write path into this data other than `POST /v1/ingest` — `architecture.md`'s `announce_task` tool was never built (out of this build order's scope, see `decisions-log.md`).

Query params: `team` (optional filter — actually filters). ⚠️ `project` is accepted for contract-compatibility but is a **silent no-op** — it returns unfiltered results rather than erroring, so a caller cannot tell it was ignored — no `project` field exists anywhere in `orgbrain-schema.md`.

```bash
curl http://127.0.0.1:8000/v1/agent-activity -H "Authorization: Bearer dev-local-token"
curl "http://127.0.0.1:8000/v1/agent-activity?team=platform" -H "Authorization: Bearer dev-local-token"
```

**Response — `200`:**

```json
{
  "results": [
    {
      "record_id": "...", "agent_id": "agent-A", "author_user_id": "u-dev", "department": "Engineering",
      "team": "platform", "session_id": "sess-vis-A", "title": "Private platform record about auth tokens",
      "status": "completed", "outcome": "insight_found", "visibility": "private",
      "created_at": "...", "gold_ref": "/v1/knowledge/..."
    }
  ]
}
```

---

## `GET /v1/handoff/{session_id}`

Latest `knowledge_entries` row for that `session_id`, full record (every column). Visibility is enforced in the same query as the lookup, so **"doesn't exist" and "exists but you can't see it" are indistinguishable** — both come back as a plain `404`, on purpose (never confirms a private session's existence to someone who shouldn't see it).

```bash
curl http://127.0.0.1:8000/v1/handoff/sess-example-1 -H "Authorization: Bearer dev-local-token"
```

**Response — `200`:** the full record — every `knowledge_entries` column (envelope + core content + extension), JSONB fields (`sensitivity_flags`, `key_points`, `domain_data`, etc.) as real nested JSON, plus `gold_ref`. See the ingest example above for the full field list — a successful `handoff` response for that same record looks exactly like what you sent it, plus server-assigned fields (`record_id`, `captured_at`, `created_at`, `consent_basis`, `consent_actor_type`, `consent_actor_id`, `schema_version`, `review_status`, `gold_ref`).

**Not found or not visible — `404`:** `{"detail": "session not found"}`

---

## `GET /v1/bus/subscribe` (SSE)

Long-lived `text/event-stream` connection. Query params: `department`/`team`/`visibility` (optional narrowing, same rule as `/v1/search`), `since` (ISO timestamp — replay events committed after this before going live).

```bash
# live-only, no replay
curl -N http://127.0.0.1:8000/v1/bus/subscribe -H "Authorization: Bearer dev-local-token"

# replay everything since a point in time, then stay live
curl -N "http://127.0.0.1:8000/v1/bus/subscribe?since=2026-08-08T10:00:00Z" \
  -H "Authorization: Bearer dev-local-token"

# narrow to one team
curl -N "http://127.0.0.1:8000/v1/bus/subscribe?team=platform" -H "Authorization: Bearer dev-local-token"
```

A real browser `EventSource`'s native reconnect (`Last-Event-ID` header) works automatically — each event's SSE `id:` field is its own timestamp, which doubles as the replay cursor.

**Event format:**

```
id: 2026-08-08T10:36:32.080227+00:00
event: created
data: {"record_id": "...", "session_id": "sess-sse-live-1", "department": "Engineering", "team": "platform", "agent_id": "agent-...", "event_type": "created", "title": "...", "summary": "...", "status": "completed", "visibility": "team", "timestamp": "2026-08-08T10:36:32.080227+00:00", "gold_ref": "/v1/knowledge/..."}

: keepalive

```

A bare `: keepalive` comment line arrives every 15s of inactivity (also doubles as the disconnect-detection interval).

> **`private` events are never delivered to anyone over this stream — not even their own author.** The compact bus payload (`orgbrain-schema.md` §5) has no `author_user_id` field, so there's nothing to check "is this mine" against. This is permanent by design, not a bug — see `decisions-log.md` (2026-08-08). `/v1/search` and `/v1/handoff` are unaffected; they query `knowledge_entries` directly, which does have `author_user_id`.

---

## MCP tools (`backend/mcp_server.py`)

Same three read operations, over MCP instead of REST — `search_knowledge`, `get_agent_activity`, `handoff` call the exact same `do_search`/`do_agent_activity`/`do_handoff` functions in `backend/app/serving.py` that the REST routes call. Not built: `record_insight`/`announce_task` (see `decisions-log.md`'s step-4/step-6 entries for why).

Run via stdio (see `orgbrain-setup.md` §5) — no REST server needs to be running for the MCP server itself, but it needs the same Postgres. Identity is fixed for the whole process via `MCP_API_TOKEN` in `.env`, not per-call (stdio has no header to carry a token).

**Tools:**

| Tool | Args | Returns |
|---|---|---|
| `search_knowledge` | `query: str`, `limit: int = 10`, `department: str \| None`, `team: str \| None` | list of result objects, same shape as `/v1/search`'s `results[]` |
| `get_agent_activity` | `team: str \| None` | list of result objects, same shape as `/v1/agent-activity`'s `results[]` |
| `handoff` | `session_id: str` | the full record (same shape as `/v1/handoff`'s `200` response); raises a tool error (not a crash) if not found/not visible |

**Example, using the official Python MCP client:**

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(command="python", args=["mcp_server.py"], cwd="backend")
async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("search_knowledge", {"query": "push notifications"})
        # result.structured_content == {"result": [...]}  for list-returning tools
        # result.is_error == True on failure (e.g. handoff on a missing/invisible session)
```

Note the SDK's actual attribute names are `structured_content`/`is_error` (snake_case) — not `structuredContent`/`isError`. A tool whose return type is a bare `list[...]` gets auto-wrapped as `{"result": [...]}` in `structured_content`, since MCP structured output must be a JSON object at the top level.
