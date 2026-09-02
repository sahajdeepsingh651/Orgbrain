# Data Passport — Running the Core Service Locally

> Status: the core service (build steps 1–6 of `data-passport-core-service.md` §7) is fully built and tested as of 2026-08-08. This doc is the map for anyone (or any fresh session) picking this repo up cold: what exists, where it lives, and the exact commands to get it running and to verify it still works.

## 1. Repo layout

```
vector-lab1/
├── docs/                        — all design docs + this one
├── docker-compose.yml           — Postgres (pgvector/pgvector:pg16), host port 5433
├── .env / .env.example          — DATABASE_URL, API_TOKENS_FILE, MCP_API_TOKEN
├── config/
│   ├── api_tokens.json          — bearer-token → identity map (gitignored, real tokens)
│   └── api_tokens.example.json  — template to copy from
├── schemas/domains/
│   └── engineering.v1.json      — the one domain_data schema that exists so far
└── backend/
    ├── requirements.txt
    ├── db/
    │   ├── schema.sql           — full Postgres schema, idempotent
    │   └── migrate.py           — applies schema.sql to DATABASE_URL
    ├── app/
    │   ├── main.py              — FastAPI app + POST /v1/ingest (the stub Gate)
    │   ├── serving.py           — GET /v1/search, /v1/agent-activity, /v1/handoff/{id}, /v1/bus/subscribe (SSE)
    │   ├── auth.py              — bearer token → Identity resolution
    │   ├── embeddings.py        — fastembed model singleton
    │   └── domains.py           — domain_data type validation (schema.md §4.0)
    ├── mcp_server.py            — standalone stdio MCP server (search_knowledge, get_agent_activity, handoff)
    └── tests/                   — integration tests exercised against a live server + DB (see §5)
```

No dashboard exists yet (React + Vite + TypeScript is the decided-but-unbuilt choice — see `data-passport-stack.md` §1).

## 2. Prerequisites

- Docker Desktop (for Postgres + pgvector)
- Python 3.11+ (3.11 is what this was built and tested against)
- Nothing else — no Node, no separate vector DB, no cloud account. See `decisions-log.md` for why.

## 3. First-time setup

```bash
# from repo root

# 1. Environment files
cp .env.example .env
cp config/api_tokens.example.json config/api_tokens.json
# .env's defaults work as-is for local dev. config/api_tokens.json needs real entries —
# see §4 for the dev identities this repo's own tests use.

# 2. Start Postgres (pgvector image, host port 5433 — see §6 for why not 5432)
docker compose up -d

# 3. Python environment + dependencies
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install --upgrade pip     # Windows; use .venv/bin/... on macOS/Linux
./.venv/Scripts/python.exe -m pip install -r requirements.txt

# 4. Apply the schema (safe to re-run any time — every statement is idempotent)
set -a; source ../.env; set +a
./.venv/Scripts/python.exe db/migrate.py
```

First install will download the `fastembed` model (`BAAI/bge-small-en-v1.5`, ~130MB) from Hugging Face once; after that it's fully offline (see `decisions-log.md`, 2026-08-08 embeddings-reversal entry).

## 4. Dev identities (`config/api_tokens.json`)

The repo's own tests assume these three exist — recreate them if you regenerate the file:

```json
{
  "dev-local-token": { "user_id": "u-dev", "department": "Engineering", "team": "platform" },
  "dev-local-token-2": { "user_id": "u-eng-2", "department": "Engineering", "team": "mobile" },
  "dev-local-token-sales": { "user_id": "u-sales-1", "department": "Sales", "team": "enterprise" }
}
```

Every `POST /v1/ingest`, `GET /v1/search`/`/v1/agent-activity`/`/v1/handoff`/`/v1/bus/subscribe` call needs `Authorization: Bearer <one of these tokens>`. See `data-passport-api-reference.md` for exactly how each endpoint uses the resolved identity, and `decisions-log.md` (2026-08-08, "Auth model for build step 4") for why tokens map to fixed identities instead of trusting a self-declared one.

## 5. Running it

**REST + SSE server:**

```bash
cd backend
set -a; source ../.env; set +a
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Do **not** add `--reload` — see §6. Confirm it's up: `curl http://127.0.0.1:8000/openapi.json`.

**MCP server** (stdio — spawned as a subprocess by an MCP client, not run standalone in a terminal for normal use):

```bash
cd backend
set -a; source ../.env; set +a
./.venv/Scripts/python.exe mcp_server.py
```

`MCP_API_TOKEN` in `.env` picks which identity the whole server process runs as (see `decisions-log.md`, 2026-08-08, "MCP server runs as one stdio subprocess per identity" — stdio has no per-call header, unlike REST). To point a real MCP client (Claude Desktop, Claude Code, etc.) at it, configure it to run `python <repo>/backend/mcp_server.py` with `cwd` set to `backend/` and the env vars from `.env` present in its process environment.

**Running the test suite** (integration tests — they need the REST server AND Postgres actually running):

```bash
cd backend
./.venv/Scripts/python.exe tests/test_ingest.py
./.venv/Scripts/python.exe tests/test_context_bus.py
./.venv/Scripts/python.exe tests/test_serving.py
./.venv/Scripts/python.exe tests/test_sse.py
./.venv/Scripts/python.exe tests/test_mcp.py       # this one spawns mcp_server.py itself — don't start it separately
```

Each test prints PASS/FAIL per case and cleans up its own rows afterward (`DELETE ... WHERE session_id LIKE 'sess-<prefix>-%'`) — re-running them is safe. See `data-passport-stack.md` §3 Build Log for what each one actually verified when it was written and why (real bugs it originally caught, not just "it passes").

## 6. Known environment quirks (verified 2026-08-08, on Windows + Git Bash)

- **Postgres host port is 5433, not 5432.** This dev machine already runs a native Postgres service on 5432; `docker-compose.yml` maps the container to 5433 instead to avoid silently connecting to the wrong server (which manifests as a confusing `password authentication failed` even though the container's credentials are correct).
- **`uvicorn --reload` is unreliable here.** The file-watcher can log `Reloading...` without the worker process ever actually respawning — the server then silently keeps serving stale pre-edit code with no error at all. If you edit code while the server's running, check the log for a fresh `Started server process [pid]` line after the edit, or just restart manually to be sure. This is why the run command in §5 doesn't use `--reload`.
- **`fastembed`'s first run downloads model weights**; if you're on a fully airgapped machine, warm the Hugging Face cache before going offline.

## 7. What's actually built vs. designed

This doc tells you how to run what exists. For what exists and how thoroughly it was verified, read (in this order):

1. `data-passport-core-service.md` — the design contract, now annotated inline with "Built (date): ..." notes wherever an endpoint's actual behavior was decided during implementation.
2. `data-passport-api-reference.md` — concrete request/response examples for every endpoint and MCP tool.
3. `data-passport-stack.md` §3 Build Log — chronological, one entry per build step, each stating exactly what was tested and what it found (including real bugs caught).
4. `decisions-log.md` — every design decision and trade-off, including ones made mid-implementation that reversed or extended what was originally documented.

All 6 build-order steps in `data-passport-core-service.md` §7 are done. Nothing beyond that (dashboard, endpoint-side capture mechanism, MCP alternatives) has been started — see `README.md`'s Current Status for the exact boundary.
