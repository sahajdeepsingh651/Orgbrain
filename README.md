# Data Passport

**One organisational brain for every AI session — and one checkpoint that stops
confidential data leaving through AI prompts.**

Two problems, one mechanism:

- Knowledge that should move across a team doesn't. Arjun can't ask for what
  Priya learned last month, because he doesn't know it exists.
- Data that shouldn't move does. Credentials and personal data leave through AI
  prompts — usually not typed by a human, but read out of a file by an agent.

Data Passport puts an interceptor between every AI coding session and the model
API. It redacts on the way out and restores on the way back, retrieves colleagues'
prior sessions when asked, and lets a session be saved to a shared Context Bus —
**but only after a human approves exactly what will be stored.**

The developer changes one environment variable and nothing else:

```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

Their tool, commands, and workflow are unchanged.

---

## What it looks like

```
1. > how should we handle push notification retries?

   [the gateway notices 3 related sessions exist and says so — titles only]

2. > ESDS_SEARCH push notification retries

   [Priya's session from last month is retrieved, DLP-scanned, and injected.
    A record you aren't allowed to see is never returned — the bus enforces
    visibility against your identity, not the gateway.]

3. > ESDS_SUBMIT

   [Claude drafts the record. The gateway validates it, redacts anything
    sensitive, and shows you exactly what would be stored:]

     ESDS Data Passport — draft a3f9c1d2 is PENDING YOUR APPROVAL.
     Nothing has been written to the Context Bus.
       title      Push retry policy
       summary    Exponential backoff, capped at 30s...
       visibility team
       redaction  1 value(s) removed (credentials=True)

4. > ESDS_APPROVE a3f9c1d2 --visibility org

   [now it is saved, and a colleague on another team can find it]
```

At no point can the AI write to organisational memory on its own. There is exactly
one call to the bus's ingest endpoint in the codebase, and it is reachable only
from a human typing `ESDS_APPROVE` in their own turn.

## Architecture

```
  developer laptop                          shared VM
  ┌──────────────────────┐                 ┌───────────────────────────┐
  │ Claude Code          │                 │  Context Bus  :8000       │
  │   ANTHROPIC_BASE_URL │                 │   FastAPI + asyncpg       │
  │        ↓             │                 │   fastembed (on-premise)  │
  │ Interceptor :8080    │──── HTTP(S) ───▶│                           │
  │   DLP, markers,      │   /v1/search    │  Postgres + pgvector :5433│
  │   approval gate      │   /v1/ingest    │                           │
  └──────────┬───────────┘                 └───────────────────────────┘
             ▼
      api.anthropic.com
```

| Component | Owns |
|---|---|
| **Human** | intent, and approval of every write |
| **AI harness** | interaction and drafting — never persistence |
| **Interceptor** (`gateway/`) | DLP, redaction/restoration, marker authorization, identity, write validation, approval, `sensitivity_flags` |
| **Context Bus** (`store/`) | persistence, semantic search, visibility/access control, handoff, audit — and stays deliberately blind to PII |

The bus never receives raw PII, by design: redaction happens on the endpoint, so
the privacy claim is structurally true rather than policy-enforced. That is
precisely why the interceptor must run on the developer's machine and not on the
VM — see `docs/ARCHITECTURE.md`.

## Quickstart

Requires Python 3.11+ and Docker.

```bash
# 1. gateway
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp store/config/account_map.example.json store/config/account_map.json
#    then put YOUR account_uuid in it — see that file's comment for how to find it

# 2. Context Bus
cd store
cp .env.example .env
cp config/api_tokens.example.json config/api_tokens.json
docker compose up -d
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; source ../.env; set +a
.venv/bin/python db/migrate.py
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. gateway, in another terminal
.venv/bin/python -m uvicorn gateway.app:app --port 8080

# 4. a session through it — NEVER export this variable
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

Do not add `--reload` to the bus; its file watcher can serve stale code silently.

## Testing

```bash
.venv/bin/python -m pytest gateway/tests -q              # 141 tests, no services needed
.venv/bin/python -m pytest gateway/tests -q -k STOPSHIP  # the one that matters most
.venv/bin/python scripts/e2e/e2e_write.py                # the demo, automated (needs the bus)
```

The stop-ship test asserts that a draft which was never approved produces **zero**
calls to `/v1/ingest`. If it goes red, the project's central claim is false.

Full guide: [`TESTING.md`](TESTING.md). Tester-facing case list:
[`docs/QA-TEST-GUIDE.md`](docs/QA-TEST-GUIDE.md).

## Layout

```
gateway/            the interceptor
  app.py            the pipeline — transport and orchestration only
  flows.py          READ / AWARENESS / WRITE; the only place that talks to the bus
  bus_client.py     REST client for the Context Bus
  pending.py        drafts awaiting human approval (cannot write to the bus)
  failure.py        failure policy as an exhaustive table
  protocol/         normalized request/response + the Anthropic adapter
  policies/         check, pii, read, markers, identity, write — all wire-agnostic
  tests/            141 tests, no services required
store/              the Context Bus (Postgres + pgvector + FastAPI + MCP read server)
scripts/            stub upstream, queue drain, live end-to-end acceptance
docs/               architecture, QA guide, findings, submissions
fixtures/           sanitised sample captures (real ones are gitignored)
```

## Security notes

This repo is about preventing leaks, so it holds itself to the same standard.

- **Never `export ANTHROPIC_BASE_URL`.** Prefix it onto a single command; exporting
  redirects every Claude Code session in that shell.
- **`fixtures/*` is gitignored except the `sample_*` files.** A real capture holds
  your `account_uuid`, `device_id`, `session_id`, and anything the agent had read
  into a `tool_result` — which is exactly the leak this project exists to stop.
- **`store/config/api_tokens.json` and `account_map.json` are gitignored.** Copy the
  `.example.json` files and fill in your own.
- **`DP_DEBUG_LOG_OUTBOUND=1` is test-only** — it writes request payloads to `/tmp`
  in plaintext.
- Use the fake `sk-test-…` shape in tests. Never a real credential.

## Honest boundaries

- Interception covers **the model API**, not "AI" in general. MCP tool calls are
  executed locally by the harness and never reach `api.anthropic.com`, so the
  gateway is structurally blind to them. MCP is therefore kept **read-only**.
- Browser sessions (claude.ai, chatgpt.com) cannot be redirected at all.
- The Context Bus trusts the endpoint's `sensitivity_flags` without verifying them.
  That is a deliberate trade — the server never receives raw PII, so it cannot
  verify, and it cannot leak. The audit log records *who asserted* each flag.
- Built for a hackathon. Identity is a static token map, not SSO.

## Documentation

| Doc | What |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | the intended system |
| [`docs/GATEWAY-OVERVIEW.md`](docs/GATEWAY-OVERVIEW.md) | what the gateway does today, with a maturity table |
| [`TESTING.md`](TESTING.md) | how to run everything |
| [`docs/QA-TEST-GUIDE.md`](docs/QA-TEST-GUIDE.md) | tester-facing cases |
| [`docs/WIRE-FINDINGS.md`](docs/WIRE-FINDINGS.md) | what real traffic actually looks like |
| [`store/README.md`](store/README.md) | the Context Bus |
| [`store/docs/decisions-log.md`](store/docs/decisions-log.md) | every design decision, with rejected alternatives |
