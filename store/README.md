# Orgbrain (ESDS Hackathon Project)

Knowledge that should move across the org doesn't. Data that shouldn't move (PII, credentials, confidential/financial data) does. Orgbrain fixes both, using a single endpoint-level checkpoint that strips PII/credentials before anything leaves the device — whether it's headed to our own knowledge base or to external AI — plus a data lakehouse that structures and serves what's left.

## Docs index

| Doc | Covers |
|---|---|
| [`orgbrain-problem-analysis.md`](orgbrain-problem-analysis.md) | The original problem statement broken into sub-problems, and which ones we're actually solving vs. deferring |
| [`orgbrain-architecture.md`](orgbrain-architecture.md) | The medallion lakehouse design (Bronze → Gate → Silver → Gold), MCP serving layer, team split |
| [`orgbrain-core-service.md`](orgbrain-core-service.md) | What we're building first for the PoC — the concrete ingest API, Context Bus mechanism, and serving API, with the endpoint-side capture/consumption mechanism left as an open, pluggable boundary |
| [`orgbrain-stack.md`](orgbrain-stack.md) | Living doc — the concrete tech stack per component, open stack decisions, and a build log updated every time something is actually implemented |
| [`orgbrain-schema.md`](orgbrain-schema.md) | The session-extraction schema — what gets pulled out of an AI session, generalized across departments |
| [`orgbrain-security-egress.md`](orgbrain-security-egress.md) | The endpoint-level checkpoint — blocking/redaction of PII, credentials, and confidential data before anything leaves the device, whether bound for Orgbrain's own ingest API or external AI |
| [`glean-research.md`](glean-research.md) | SOTA analysis — how Glean (closest mature analog) is architected, what we adopted, what we didn't |
| [`decisions-log.md`](decisions-log.md) | Every architectural/design decision made, with reasoning and rejected alternatives — updated as we go |

## Current status

- Architecture, schema, and security design: drafted, one round of SOTA-informed revision done, plus a policy-configuration model (admin floor / employee ceiling) applied across both gates.
- PII/credential detection & redaction relocated entirely to the endpoint device (2026-08-07) — the central system never scans for or stores raw PII; it trusts endpoint-reported `sensitivity_flags` metadata only. See `orgbrain-architecture.md` § The Endpoint Checkpoint and `orgbrain-security-egress.md`.
- **Core service PoC build is complete (2026-08-08)**, all 6 steps of `orgbrain-core-service.md` §7: Postgres schema; `POST /v1/ingest` with a stub Gate; Context Bus write + `NOTIFY`; `GET /v1/search`/`/v1/agent-activity`/`/v1/handoff`; `GET /v1/bus/subscribe` (SSE); MCP tool wrappers (`search_knowledge`/`get_agent_activity`/`handoff`, stdio transport). Every step tested against a live running server/DB, not just code review — see `orgbrain-stack.md` §3 Build Log for exactly what was verified and how, and `decisions-log.md` for the real gaps and trade-offs found along the way (notably: the accepted HNSW/access-control ordering gap in search, and `private`-visibility events never reaching anyone over the SSE bus, author included). Stack: Python + FastAPI + `asyncpg`, `fastembed` for fully on-premise embeddings (no external API calls), Docker Compose + `pgvector`.
- Endpoint-side capture/consumption mechanism (browser extension / network proxy / IDE plugin / manual) is still intentionally left open and decoupled behind the core's plain HTTP contract — not started, not this workstream's build.
- Dashboard (React + Vite + TypeScript, decided) — not built yet; not part of the core-service build order.
- Open items: concrete PII/confidential-data regex rules not yet written (categories only); Egress Gate interception mechanism (local proxy vs. browser extension vs. IDE plugin) proposed but not confirmed; concrete admin-floor list (which destinations/categories/session-types are actually mandatory) not yet defined, just the mechanism for defining them — see `orgbrain-security-egress.md`.
