# Data Passport — Architecture Doc (Medallion Lakehouse)

## 1. The idea in one line

Knowledge that should move across the org doesn't. Data that shouldn't move (PII, credentials, customer data) does. **Data Passport fixes both at once**: a single on-device checkpoint strips PII/credentials before anything leaves the endpoint — whether it's headed to our own knowledge base or to an external AI — and a data lakehouse structures and serves whatever's left.

> Updated 2026-08-07 with ideas adopted from researching Glean's architecture — see `glean-research.md` for full findings and what we chose not to copy. Updated again 2026-08-07: PII/credential detection & redaction moved fully to the endpoint device — see § The Endpoint Checkpoint and `decisions-log.md`.

## 2. Background: warehouse vs. lake vs. lakehouse

| Pattern | Storage model | Strength | Weakness for us |
|---|---|---|---|
| Data Warehouse | Structured, schema-on-write (tables) | Fast to query, dashboard-friendly | Org knowledge isn't clean rows — it's chat logs, docs, decisions in prose. Forces ingestion adapters before anything lands. |
| Data Lake | Raw files/blobs, schema-on-read | Captures everything cheaply, no upfront schema | No natural checkpoint to enforce PII/credential redaction. Search and governance on raw data is weak. |
| Data Lakehouse | Raw layer + governed curated layer + serving layer (medallion: Bronze/Silver/Gold) | Combines both: cheap raw capture AND a governed, queryable curated layer | Full versions (Iceberg/Delta + Spark/Trino) are heavy for a hackathon. We use a lightweight version. |

We're using the **lakehouse / medallion pattern**, lightly implemented, because the layer transitions map directly onto the "passport" metaphor.

## 3. The layers

### The Endpoint Checkpoint — where passport control actually happens

Before Bronze, before anything leaves the device at all: a single on-device detection & redaction engine inspects outbound content and strips PII/credentials — regardless of destination. This is the real passport-control moment; everything downstream (Bronze/Gate/Silver/Gold) operates on already-clean content.

- **One shared engine, two outbound flows** — the same on-device library is invoked whether content is headed toward Data Passport's own ingest API (always redact, no exceptions) or toward an external AI (destination-based policy, `data-passport-security-egress.md` §2). This doc previously described these as separate checkpoints (a server-side "Ingestion Gate" and an endpoint-side "Egress Gate"); they've since been unified — see `decisions-log.md`.
- Detection: regex + pattern matching for secrets (API keys, tokens, connection strings — gitleaks-style patterns) and PII (emails, phone numbers, names, customer IDs — regex plus a lightweight NER pass, e.g. Presidio).
- Redaction: flagged spans are stripped or replaced with placeholders (`[REDACTED_EMAIL]`) before the request ever leaves the device — never silently deleted, since the fact that something was caught is itself useful signal.
- What actually reaches the server is metadata only, never the raw flagged values: `sensitivity_flags` (`contains_pii`, `contains_credentials`, `redaction_applied`, `redaction_count`) travels with the redacted content so the central audit trail can be populated without the central system ever seeing what was caught.
- **Team decision (2026-08-07): no server-side PII scanning, by design.** The central system trusts the endpoint's redaction completely and never independently re-scans incoming content. This keeps the privacy claim structurally true — "we never receive it, so we can't leak it, not even to our own admins" — rather than merely policy-enforced. Accepted trade-off: there is no central backstop if an endpoint's detection has a bug, is an outdated version, or is bypassed. Not hidden — see `decisions-log.md`.
- Build ownership: this engine lives inside whatever endpoint-side software the team eventually builds (browser extension / network proxy / IDE plugin — still undecided, see `data-passport-security-egress.md` §4–5). It is explicitly **not** part of the core service being built first (`data-passport-core-service.md`).

### Bronze — Raw / Unfiltered

- Everything lands here exactly as captured: agent conversation logs, meeting notes, Slack/doc exports, decision write-ups, code review comments — whatever a team or an AI agent produces.
- Schema-on-read: no structure enforced, no domain validation. This is intentional — capture must be low-friction or people/agents won't feed it.
- **PII and credentials are NOT present here.** Passport control for sensitive data already happened at the endpoint (above) before this content ever left the device — Bronze is "raw" only in the sense of unstructured and unvalidated, never in the sense of containing sensitive data.
- Storage: a folder structure or MinIO (S3-compatible object storage) bucket on the VM, partitioned by date/team/source, e.g. `bronze/{team}/{source}/{yyyy-mm-dd}/{id}.json`.

### The Gate — Bronze → Silver (Structuring & Validation)

Nothing reaches Silver without passing through it — but PII/credential detection is no longer this checkpoint's job; that already happened at the endpoint, above.

1. **Provenance tagging** — every record is stamped with team, author, agent/session ID, source system, and timestamp.
2. **`domain_data` type validation** — reject with a clear error on a type mismatch against the domain's declared schema (`data-passport-schema.md` §4.0).
3. **Structuring / extraction** — the (already-redacted) text is turned into a knowledge record: title, summary, tags, team, links to source.
4. **Audit logging** — the endpoint's reported `sensitivity_flags` metadata (what was flagged and how much, never the actual values) plus any validation failures are written to `redaction_audit_log`. Still your demo evidence — "here's what tried to cross the border and didn't" — just sourced from endpoint-reported metadata instead of a central scan.

Anything that fails validation is quarantined (stays in Bronze, flagged) rather than rejected silently — someone can review and override.

### Silver — Cleaned, Structured, Safe

- One record per captured insight/decision/session, PII-free, tagged with provenance.
- Deduplicated against near-identical recent entries (this is also where a contradiction-detection pass could later hook in, as a stretch feature).
- Storage: PostgreSQL tables (`knowledge_entries`, `redaction_audit_log`, `agent_sessions`).

### Gold — Curated, Indexed, Servable

- Silver records get embedded (vector representation of their content) and indexed for semantic search, not just keyword search.
- Aggregated views built here: a team knowledge base, a cross-team decision registry, an "agent activity ledger" (what any AI agent is working on right now, and what it left off with — enabling another agent/session to pick up where it stopped).
- Storage: PostgreSQL + `pgvector` extension — same database as Silver, just additional tables/indexes (`knowledge_embeddings`, `agent_activity`). Keeping Silver and Gold in one Postgres instance keeps the VM setup to a single service.
- **Ranking uses an HNSW index (`pgvector`)** on `knowledge_embeddings`, chosen to demonstrate a production-appropriate ANN approach from the start rather than an unindexed exact scan, even though hackathon data volume (tens–low hundreds of records) wouldn't strictly require one. `visibility`/`department`/`team` access control is filtered on the candidate set HNSW returns, **not before it** — a known, accepted gap: a permitted, relevant match can be silently dropped if it falls outside the initial ANN candidate window, and an empty result is then indistinguishable from a genuine "nothing exists" response. Deferred fix, tracked post-hackathon: upgrade to `pgvector` ≥0.8.0 and enable iterative index scans (`hnsw.iterative_scan`). Full reasoning and the rejected exact-scan alternative in `decisions-log.md` (2026-08-07).
- Glean's hybrid ranking (vector + graph-relationship + activity signals) was considered and reverted on 2026-08-07: it solves a relevance problem that only shows up at Glean's scale (billions of docs), and building/tuning a scoring formula for it would spend hackathon time on a problem the demo doesn't actually have. Revisit post-hackathon if the knowledge base grows large enough for plain vector ranking to start returning noisy results.

### Consent model — which sessions actually get linked to the passport

Resolved 2026-08-07 (previously an open question, see `decisions-log.md`). Not every session a user or agent runs becomes a permanent Silver/Gold record — whether it does is governed by consent, not automatic capture:

- **Admin policy sets a mandatory floor.** Certain categories are always captured regardless of individual choice — e.g. "all Engineering incident-response sessions," or "any session whose `outcome` is `decision_made`." Defined as a small set of policy rules (department, outcome, source_system match conditions), maintained by the security/admin team.
- **Employee choice governs everything else, additively only.** The employee can choose to link additional sessions beyond the mandatory floor; they cannot exempt a session that policy already mandates.
- **Mechanically, this reuses the pipeline we already have**: a session only becomes a Silver/Gold record when `record_insight` is called.
  - Admin-mandated categories → `record_insight` fires automatically at session end (employee is notified why, for transparency — not a silent capture).
  - Everything else → the employee (or their agent, with confirmation) calling `record_insight` **is** the opt-in. No call, no shared record. The session can still exist locally for the employee's own reference/handoff use — it just never crosses into Bronze.
- Every resulting record carries `consent_basis` (`admin_mandated` | `user_opted_in`) and `consent_actor` (which policy rule, or which employee) — see `data-passport-schema.md` §A — so the audit trail shows not just what was captured but why it was allowed to be.

This is the same admin-floor/employee-ceiling shape used for the Egress Gate's destination and category selection (`data-passport-security-egress.md` § Policy configuration model) — one consistent pattern across both gates rather than a different consent mechanism per checkpoint.

## 4. Serving layer — how agents and teams actually use it

An **MCP server** sits on top of Gold and exposes tools any connected AI agent can call:

- `search_knowledge(query)` — semantic + keyword search across all teams' curated knowledge.
- `record_insight(content, team, tags)` — write a new entry (goes through the Gate before landing in Silver/Gold).
- `announce_task(agent_id, task, status)` — an agent broadcasts what it's currently working on, into the activity ledger.
- `get_agent_activity(team | project)` — see what any agent, anywhere in the org, is working on right now.
- `handoff(session_id)` — pick up the context/state another agent session left off with.

A lightweight web dashboard reads the same Gold tables to visualize: live agent activity feed, knowledge search UI, and the redaction audit log (the "what didn't cross the border" view) — this last one is the strongest visual proof of the "Data Passport" concept for judges.

## 5. End-to-end flow

```
Team / AI Agent (endpoint device)
      │  (raw capture: conversation, doc, decision, code note)
      ▼
 ENDPOINT CHECKPOINT  (detect PII/secrets → redact — nothing leaves the device unclean)
      │
      ▼
 BRONZE  (object storage / folders — unstructured, but already PII-free)
      │
      ▼
 THE GATE  (validate domain_data types → tag provenance → structure/extract → audit log from endpoint-reported flags)
      │
      ├──► quarantined (failed validation, held for review)
      │
      ▼
 SILVER  (Postgres — clean, structured, provenance-tagged knowledge records)
      │
      ▼
 GOLD  (Postgres + pgvector — embedded, indexed, aggregated views + agent activity ledger)
      │
      ▼
 MCP SERVER + DASHBOARD  (search_knowledge, record_insight, announce_task, get_agent_activity, handoff)
      │
      ▼
Any team / any AI agent, anywhere in the org, anytime
```

## 6. Why this is a lightweight lakehouse, not a full one

We deliberately skip:
- Iceberg/Delta table format (ACID, time-travel, schema evolution at the storage layer)
- Spark/Trino distributed query engines

Because with a 4-person team on a hackathon clock, that infrastructure is pure setup risk with no demo payoff. A folder/MinIO Bronze layer + a Postgres Silver/Gold layer preserves the medallion story (raw → governed → served) and the passport-control narrative, while being buildable and demo-stable in the time available.

## 7. Suggested team split (4 people)

1. **Endpoint Checkpoint (detection & redaction)** — build the on-device PII/secret detection engine and redaction logic; shared by both the ingest-bound flow and the Egress Gate's AI-bound flow (`data-passport-security-egress.md`).
2. **Bronze + Gate (ingestion, validation, structuring)** — raw capture format, `domain_data` type validation, provenance tagging, structuring/extraction, audit logging from endpoint-reported metadata.
3. **Silver + Gold (data model & search)** — Postgres schema, pgvector embeddings, dedup logic, aggregation views.
4. **MCP server + Dashboard** — expose the tools (`search_knowledge`, `record_insight`, `announce_task`, `get_agent_activity`, `handoff`) and build the demo UI: knowledge search view, live agent activity feed, and the redaction audit log view (the visual "proof" of the theme).

## 8. What to say to judges, in one breath

"We built passport control at the earliest possible point: the device itself. PII and credentials are stripped before anything ever leaves the endpoint — whether it's headed to our own knowledge base or to an external AI — so our servers never receive, store, or even see raw sensitive data in the first place. What does travel lands in Bronze clean, gets structured and validated at the Gate, and becomes searchable in Silver and Gold. The result: knowledge travels freely across teams and AI agents, and the things that shouldn't travel, never even leave the building."
