# Codebase map — every file, and why it exists

One line per file: **what it's for**, not what every function does. Line counts are source
lines. 125 files are tracked by git; everything large is correctly gitignored.

Three programs live here:

| Directory | Program | Runs on | Port |
|---|---|---|---|
| `gateway/` | **the Interceptor** | developer laptop | 8080 |
| `store/` | **the Context Bus** | shared VM | 8000 (Postgres 5433) |
| `dashboard/` | **the admin UI** | laptop, browser | 5173 |

---

# `gateway/` — the Interceptor (3,277 lines)

The middleman between Claude Code and `api.anthropic.com`. Everything security-relevant
happens here.

## Top level

| File | Lines | Why it exists |
|---|---:|---|
| **`app.py`** | 461 | **The entry point and the whole pipeline.** `proxy()` is the one function that shows the real order of operations. Also holds the SSE relay, the dashboard's own SSE endpoint, and usage logging. **If you read one file, read this.** |
| **`flows.py`** | 552 | The four flows as orchestration: READ, AWARENESS, WRITE-request, WRITE-response. Exists separately because `policies/` is pure (no I/O) and `app.py` is transport — retrieval orchestration in either would bury the one security-critical ordering decision. Contains **both** calls to the bus's ingest endpoint. |
| `bus_client.py` | 163 | The HTTP client for the Context Bus. REST, deliberately *not* MCP — MCP binds one identity per process, wrong shape for a proxy serving many developers. The only place the gateway may talk to the bus. |
| `pending.py` | 188 | **The approval gate, on disk.** Drafts park in `/tmp/dp_pending` as JSON until a human approves. Nothing in this file can write to the bus — there's no bus import. Also holds the idempotency-key hash. |
| `failure.py` | 128 | Failure semantics as a **table**: 5 flows × 6 failure modes = 30 explicit decisions, with a test asserting exhaustiveness. Exists so "what happens when the bus is down mid-write?" is answerable by reading a table instead of hunting `except` branches. |
| `tap.py` | 101 | **Historical.** The original T0 tool — captured request bodies to `fixtures/` and forwarded nothing. How the wire format was discovered. Superseded by `app.py`; not part of the running system. |
| `cli.py` | 15 | `argparse` wrapper so you can launch the Interceptor without typing a uvicorn command. |
| `__init__.py` | 0 | package marker |

## `gateway/protocol/` — wire formats (390 lines)

The only code allowed to know what a request looks like on the wire.

| File | Lines | Why it exists |
|---|---:|---|
| `normalized.py` | 71 | The provider-neutral internal types (`NormalizedRequest` / `Response`). Policies see only these. Mostly dataclasses — read the docstring, skip the code. |
| `detect.py` | 50 | Picks an adapter from the request. Four tiers, cheapest signal first; tiers 1 and 4 are real, 2–3 are honest stubs. Detects the **wire protocol, never the harness**. Unrecognised → raw passthrough. |
| **`anthropic_adapter.py`** | 269 | Every Anthropic-specific detail. Two things worth your time: the **model gating** (which models accept a literal mid-conversation `role:"system"` vs. a `<system-reminder>` folded into the user turn — Sonnet 5 takes the fallback, so that's your live path), and the **byte-fidelity passthrough** that re-emits untouched requests verbatim to protect the prompt cache. Also extracts your `session_id` and `account_uuid` from the JSON-string `metadata.user_id` Claude Code already sends. |
| `__init__.py` | 0 | package marker |

## `gateway/policies/` — the decisions (1,279 lines)

Pure logic, no I/O. Every one of these operates only on `NormalizedRequest`.

| File | Lines | Why it exists |
|---|---:|---|
| **`markers.py`** | 186 | **Your security boundary.** Decides whether `ESDS_*` is authorized — by *position*, not presence: last genuine human turn, `type=="text"` blocks only, line-anchored. This is what stops a repo file containing `ESDS_APPROVE <id>` from publishing to your org brain. **Read this twice.** |
| `pii.py` | 363 | **The real DLP suite.** Nine detectors with checksum validation (Aadhaar/Verhoeff, card/Luhn, GSTIN/mod-36, phone via libphonenumber), plus a JSON field-name pass for `tool_result` records. Regex + math, no ML, fully offline. |
| `check.py` | 192 | The redact/restore **mechanism** — the token vault, and `StreamRestorer` for boundary-aware restoration mid-SSE. Its own detector is one deliberately fake pattern (`sk-test-…`); `pii.py` is the real suite built on this machinery. Don't confuse the two. |
| `write.py` | 277 | The draft contract: the extraction prompt sent to the model, schema validation, `sensitivity_flags` derived from the vault (not asserted by the model), and the human-facing approval render. Also holds the list of fields the model is **forbidden** to decide — `session_id`, `visibility`, `captured_by`. |
| `read.py` | 162 | `is_new_human_turn()` — the trickiest predicate you own, and the reason injection works on real traffic. Plus the two renderers (`render_documents` for explicit search, `render_awareness` for titles-only) and the relevance floors. `apply()` is test scaffolding, not the retrieval path. |
| `identity.py` | 99 | Maps `account_uuid` → a bus token + user/department/team, from `store/config/account_map.json`. The hackathon stand-in for real `dp_*` key issuance. Fails closed on unknown accounts — **except** under `DP_DEMO_MODE=1`. |
| `__init__.py` | 0 | package marker |

## `gateway/tests/` — 2,205 lines

Named by the milestone they cover. These are real unit tests with a 258-line `conftest.py`.

| File | Lines | Covers |
|---|---:|---|
| `conftest.py` | 258 | fixtures, fake bus, request builders |
| `test_g1_identity.py` | 135 | account_uuid extraction and mapping |
| `test_g2_markers.py` | 195 | positional marker authorization — the injection defense |
| `test_g4_retrieval.py` | 268 | the READ flow and scan-before-inject |
| `test_g5_awareness.py` | 200 | the unprompted awareness probe |
| `test_g6_write.py` | 480 | draft extraction, validation, the approval gate |
| `test_g7_streaming.py` | 268 | SSE relay and mid-stream token restoration |
| `test_g8_failures.py` | 267 | the failure table |
| `test_gp_pii_fixes.py` | 190 | the four confirmed PII detector fixes |
| `test_gt_passthrough.py` | 144 | byte-fidelity when nothing was touched |
| `data/account_map.test.json` | — | test identity fixture |

---

# `store/` — the Context Bus (1,022 lines + schema)

FastAPI + asyncpg over Postgres 16 + pgvector. Deliberately **blind to PII** — it never
receives raw values, which is what makes the privacy claim structural rather than policy.

## `store/backend/app/`

| File | Lines | Why it exists |
|---|---:|---|
| **`main.py`** | 321 | The app, and `POST /v1/ingest` — the only write endpoint. Bronze staging, validation, embedding, then one transaction writing the record + embedding + audit row + bus event + `pg_notify`. Also idempotency handling. |
| **`serving.py`** | 322 | All five read endpoints: `/v1/search` (the hybrid vector+keyword query), `/v1/agent-activity`, `/v1/handoff/{session_id}`, `/v1/knowledge/{id}`, and `/v1/bus/subscribe` (SSE). **Holds `VISIBILITY_CLAUSE`** — the query-time ACL enforcement that runs on every retrieval. |
| `auth.py` | 51 | Bearer token → `Identity(user_id, department, team)`, from a static JSON file. Cached at module scope, so adding a token needs a process restart. |
| `domains.py` | 58 | Validates `domain_data` against a JSON schema in `store/schemas/domains/`. The extensibility hook — one schema exists. |
| `embeddings.py` | 10 | Lazily builds the `fastembed` model. Ten lines because that's genuinely all it takes: `TextEmbedding()` resolves to `BAAI/bge-small-en-v1.5`, 384-dim, CPU, offline. |
| `__init__.py` | 0 | package marker |

## `store/backend/db/`

| File | Lines | Why it exists |
|---|---:|---|
| **`schema.sql`** | 162 | Five tables. `knowledge_entries` (the records), `knowledge_embeddings` (384-dim vectors + HNSW cosine index), `redaction_audit_log` (who asserted the PII flags), `context_bus_events` (the compact append-only feed the SSE endpoint replays), and `agent_activity` — **which nothing reads or writes.** |
| `migrate.py` | 29 | Applies `schema.sql` in one idempotent execute. That's the whole migration system. |
| `__init__.py` | 1 | package marker |

## `store/backend/` top level

| File | Lines | Why it exists |
|---|---:|---|
| `mcp_server.py` | 67 | An MCP wrapper over three of the six read endpoints, stdio only. Binds **one identity per process** from `MCP_API_TOKEN` — which is exactly why the gateway uses REST instead. Not in compose; launched by hand. |
| `Dockerfile` | — | Builds the backend image. **Has never successfully run** — see `DEMO-RISKS.md` #12. |
| `requirements.txt` | — | fastapi, asyncpg, fastembed, phonenumbers, mcp |

## `store/` data and config

| Path | Why it exists |
|---|---|
| `docker-compose.yml` | Postgres + pgvector on host port **5433**, plus a `backend` service that doesn't work. In practice: compose for Postgres only, uvicorn natively. |
| `config/api_tokens.json` | bus token → user/department/team. Gitignored; `.example.json` is the template. Four identities live, including yours. |
| `config/account_map.json` | `account_uuid` → bus token. Read by the **gateway**, not the store. |
| `schemas/domains/engineering.v1.json` | The one domain schema — 5 fields. |
| `bronze/` | **Raw payload staging.** Every ingest body written verbatim *before* validation, at `{team}/{source}/{date}/{id}.json`. Write-only forensics — nothing reads it, there is no Bronze→Silver job. Currently 202 files, mostly test junk. |
| `.env` / `.env.example` | `DATABASE_URL`, `BRONZE_DIR`, tokens path |
| `offline_images.tar` | Docker images for air-gapped VM setup. Gitignored. |
| `docs/` | 10 files, 1,164 lines — the *original* design docs, written before the gateway existed. Where `record_insight` and the TLS-MITM spike come from, both of which the built system superseded. Read as history, not spec. |

## `store/backend/tests/` — 1,320 lines

⚠️ These are **integration scripts, not unit tests** — each needs a live server on
`127.0.0.1:8000` and real Postgres. No conftest, no in-process client. Consequence: the
visibility SQL and the search ranking have **no isolated coverage.**

`test_ingest.py` · `test_serving.py` · `test_sse.py` · `test_mcp.py` ·
`test_embeddings.py` · `test_context_bus.py` · `test_idempotency_race.py`

---

# `dashboard/` — the admin UI (493 lines)

Vite + React + Tailwind. Talks to both the gateway and the bus directly from the browser.

| File | Lines | Why it exists |
|---|---:|---|
| `src/App.jsx` | 81 | Shell — sidebar, top bar, three tabs. The top-bar search box and the profile avatar are **decorative**. |
| `src/components/XRayMonitor.jsx` | 173 | **The best thing in the UI.** Subscribes to the gateway's own SSE (`:8080/v1/dashboard/stream`) and shows raw vs. sanitized side by side — what you typed vs. what Anthropic received. |
| `src/components/ContextBusExplorer.jsx` | 115 | Card grid of passports from `GET :8000/v1/search`. Three problems: hardcoded to the wrong identity, `q='.*'` matches nothing, fetches once and never refreshes. Filter/Sort/New Context buttons are dead. See `SEARCH-AND-UI-PLAN.md` Part 4. |
| `src/components/ApprovalInbox.jsx` | 114 | Pending drafts from `GET :8080/v1/dashboard/pending` — the human-in-the-loop view. |
| `src/main.jsx` | 10 | React root |
| `src/App.css`, `src/index.css` | — | Tailwind + Material-3 design tokens |
| `vite.config.js`, `package.json`, `.oxlintrc.json` | — | build config |
| `dist/` | — | a stale build output; `npm run dev` is what the cheatsheet uses |

---

# `scripts/` — 901 lines

| File | Lines | Why it exists |
|---|---:|---|
| `stub_upstream.py` | 173 | **A fake Anthropic API.** Lets you test the whole gateway with no API key and no cost, and it chunks SSE at 4 bytes specifically to split the multi-byte redaction delimiters across boundaries. Genuinely clever. |
| `e2e/e2e_write.py` | 158 | The full WRITE round trip against a live bus — the "7/7 passing" demo proof. |
| `e2e/e2e_read.py` | 137 | The full READ round trip. |
| `setup_identity.py` | 113 | Finds your `account_uuid` and writes the `account_map.json` entry. Run this once. |
| `build_measurement_table.py` | 104 | Turns usage logs into the latency/cache measurement table. |
| `drain_queue.py` | 62 | Manual sweep for writes queued while the bus was down. The gateway also drains opportunistically per request. |
| `package_for_vm.py` | 56 | Bundles everything for the air-gapped VM. |
| `download_model.py` | 15 | Pre-seeds `offline_model_cache/` with the embedding model so the bus never needs the network. |
| `replay.sh` | — | replays captured fixtures through the gateway |
| `test_e2e_gateway.py` | 41 | ad-hoc harness |
| `test_find_draft.py` | 40 | ad-hoc harness |

---

# `fixtures/` — real captured traffic

Six JSON files: actual Claude Code request bodies captured by `tap.py`. **This is how the
wire format was reverse-engineered** — the `metadata.user_id` JSON-string discovery came
from here. Gitignored; may contain real content.

---

# `docs/` — 2,402 lines

| File | Lines | What it is |
|---|---:|---|
| **`ARCHITECTURE.md`** | 626 | The design document. Written *before* the build, so read it as intent — some sections (the dp_* key model, contradiction detection) describe things that don't exist. |
| `QA-TEST-GUIDE.md` | 398 | Tester-facing, ~40 cases, with the stub upstream. |
| `GATEWAY-OVERVIEW.md` | 375 | What actually exists, with a maturity table and known issues. **The most honest doc in the repo.** |
| `PII-PROGRAM.md` | 236 | The DLP build plan |
| `TEST-PLAN.md` | 250 | Your own T0–T5 validation ladder. *Not* the QA guide — different artifact. |
| `PII-CAPABILITIES.md` | 162 | What the detectors catch |
| `WIRE-FINDINGS.md` | 160 | What was learned from `fixtures/` — the `metadata.user_id` shape, prompt-cache breakpoints. |
| `QA-FINDINGS.md` | 93 | Defects found in QA (the numbered ones referenced in code comments) |
| `scope.md` | 6 | scope note |
| `submissions/approach.md`, `submissions/infrastructure.md` | — | hackathon submission text |
| ⚠️ `DEMO_CHEATSHEET.md` | 96 | **Stale duplicate — delete it.** Tells you to type `ESDS_APPROVE` with no ID, which fails. |

---

# Root

| File | What it is |
|---|---|
| `README.md` (187) | The pitch, the architecture diagram, the quickstart. Contains the "exactly one ingest call" claim that grep disproves. |
| **`DEMO_CHEATSHEET.md`** (109) | **The correct one.** Four terminals, three demo scenarios. |
| `TESTING.md` (189) | how to run the suites |
| `SETUP_GUIDE.md` (79) | first-time setup |
| `DEMO-RISKS.md` | 13 verified findings ranked by demo risk |
| `SEARCH-AND-UI-PLAN.md` | search/capture/UI fixes + the READ and WRITE trace runbooks |
| `CODEBASE-MAP.md` | this file |
| `pyproject.toml`, `requirements.txt` | gateway deps |
| ⚠️ `test_json.py` (2.2K) | **Scratch leftover.** Tests `write.find_draft` against a pasted response. Note its sample uses `"outcome": "issue_found"`, which isn't a valid enum value — so it doesn't even reflect the real contract. Delete. |
| ⚠️ `demo_slide` | **A 1-byte file containing one newline.** Delete. |
| `offline_model_cache/` | the pre-downloaded embedding model (gitignored) |
| `.claude/settings.local.json` | Claude Code permissions for this repo |

**Gitignored blobs in your working tree** — correctly excluded, but 625MB of local weight:
`data_passport_store.tar.gz` (495M), `docker-24.0.5.tgz` (70M), `docker-compose` (60M
binary), `offline_model_cache.tar.gz` (61M), `store/offline_images.tar`.

---

# The wiki (outside the repo)

`~/obsidian_vault/projects/hackathon_agent_layer/` — knowledge layer, no code.

| File | What it is |
|---|---|
| `SCHEMA.md` | Rules for any LLM maintaining the wiki |
| `wiki/index.md` | Page catalog + **two flagged contradictions** + gaps |
| `wiki/log.md` | Append-only history of ingests and builds |
| `wiki/architecture/system-design.md` | The three-component split and the two load-bearing orderings |
| `wiki/concepts/awareness-vs-retrieval.md` | Why telling someone knowledge exists ≠ handing it to them |
| `wiki/concepts/marker-authorization.md` | Why position, not presence, authorizes |
| `wiki/decisions/gateway-over-mcp.md` | Why the interceptor is mandatory and MCP stays read-only |
| `wiki/decisions/approval-gate.md` | Why validation is not approval |
| `wiki/decisions/deployment-topology.md` | Why the interceptor runs on the laptop |
| `wiki/sources/glean.md` | Glean teardown with source classes |

⚠️ Every page is tagged `llm-framed`. The **spine sentences, the verdict lines in
`decisions/`, and all `[[links]]` are blank and waiting for you** — by design, per your own
schema. The LLM doesn't write the frame.

---

# Where to actually start

1. **`gateway/app.py`** — `proxy()`. The whole pipeline in one function.
2. **`gateway/policies/markers.py`** — your security boundary.
3. **`store/backend/app/serving.py`** — `VISIBILITY_CLAUSE` and the search query.

Those three files hold most of the decisions that make this system what it is.

## Files that are dead or historical

- `gateway/tap.py` — how the wire format was found; not in the running system
- `store/backend/mcp_server.py` — works, not deployed, 3 of 6 capabilities
- `agent_activity` table — nothing reads or writes it, 4 indexes maintained on it
- `store/bronze/` — write-only; there is no consumer
- `dashboard/dist/` — stale build
- `test_json.py`, `demo_slide`, `docs/DEMO_CHEATSHEET.md` — **delete these three**
