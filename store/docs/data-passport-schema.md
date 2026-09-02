# Data Passport — Session Extraction Schema

> Status: DRAFT — update this file whenever the schema changes. Bump `schema_version` on any breaking change and note it in the Changelog at the bottom.
>
> Updated 2026-08-07 with ideas adopted from Glean's architecture — see `glean-research.md` for full findings.

## 1. Design principle

Envelope + extension pattern (same idea as CloudEvents). A small set of fields is **universal** — every department's session produces them, and cross-team search / the context bus can always rely on them existing. Everything department-specific lives in a separate, namespaced payload so no department is forced into a lowest-common-denominator shape, and no department's quirks leak into the shared schema.

A record has three parts:
- **A. Envelope** — identity & governance (required, universal)
- **B. Core content** — the semantic payload that actually travels (required, universal)
- **C. Extension** — domain-specific structured data (optional, namespaced per department)

This schema is populated at the **Bronze → Silver** step (see `data-passport-architecture.md`): an extractor reads the session transcript — already redacted at the endpoint before it ever reached Bronze, see `data-passport-architecture.md` § The Endpoint Checkpoint — and fills in A + B + C.

## 2. Full schema

### A. Envelope (universal, required)

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Version of this schema the record conforms to, e.g. `"1.0.0"` |
| `record_id` | uuid | Unique id of this extracted knowledge record |
| `session_id` | string | Pointer back to the raw Bronze transcript |
| `source_system` | string | e.g. `claude-code`, `copilot`, `internal-chatbot`, `support-tool` |
| `department` | controlled vocab | See §3 |
| `team` | string | Finer-grained than department |
| `author` | object `{user_id, role}` | Human owner of the session |
| `agent_id` | string \| null | Which AI agent/model ran it, if any |
| `started_at` | timestamp | Session start |
| `ended_at` | timestamp | Session end |
| `captured_at` | timestamp | When this record was extracted into Bronze |
| `visibility` | enum | `private` \| `team` \| `department` \| `org` — who/what can query it downstream |
| `consent_basis` | enum | `admin_mandated` \| `user_opted_in` — why this record exists at all. Set at the moment `record_insight` is called: automatically for admin-mandated categories, or by the employee's own choice otherwise. See `data-passport-architecture.md` § Consent model |
| `consent_actor` | object `{type, id}` | `type` = `policy_rule` (with the rule id) if `admin_mandated`, or `type` = `user` (with the employee's id) if `user_opted_in` — audit trail for who/what caused this session to be linked to the passport |
| `sensitivity_flags` | object | `{contains_pii: bool, contains_credentials: bool, redaction_applied: bool, redaction_count: int}` — populated by the endpoint's detection engine before transmission and passed through at ingest; the central Gate does not independently verify these (zero server-side PII scanning, by design — see `decisions-log.md`) |
| `status` | enum | `in_progress` \| `completed` \| `blocked` \| `handed_off` \| `abandoned` |
| `links` | array of `{type, target_id}` | `type` ∈ `continues_from`, `supersedes`, `related_to`, `contradicts`, `blocked_by` |

### B. Core content (universal, required)

| Field | Type | Description |
|---|---|---|
| `title` | string | Short human-readable label |
| `summary` | string | 1–3 sentence distillation — the field a stranger reads first |
| `intent` | string | What the session was trying to accomplish |
| `outcome` | enum | `decision_made` \| `insight_found` \| `issue_resolved` \| `blocker_hit` \| `question_open` \| `in_progress` |
| `outcome_detail` | string | Free text elaboration of the outcome |
| `key_points` | array of strings | Atomic, reusable facts — smallest unit worth surfacing to another team |
| `next_steps` | array of strings | Required for handoff — what should happen next |
| `open_questions` | array of strings | Unresolved items another agent/session could pick up |
| `entities` | array of `{type, value}` | Tags: technology, product, error_code, system, etc. — controlled `type`, freeform `value` |
| `artifacts` | array of `{type, ref}` | Pointers (not content) to code, docs, tickets, PRs, dashboards |
| `review_status` | enum | `auto_extracted` \| `human_verified` |

### C. Extension (domain-specific, optional)

| Field | Type | Description |
|---|---|---|
| `domain` | string | Namespace + version, e.g. `engineering.v1` |
| `domain_data` | object | Validated against that domain's own typed schema (see §4) |

## 3. Controlled vocabularies (must be agreed org-wide)

These three must not drift per team, or cross-department search breaks:

- `department` — e.g. `Engineering, Support, Sales, Data, Marketing, HR` (finalize list with the team)
- `entities[].type` — e.g. `technology, product, error_code, customer_segment, system` (finalize list)
- `outcome` — fixed enum above; do not extend per department, use `outcome_detail` instead

## 4. Domain extension examples

Each department owns and versions its own `domain_data` shape independently — no org-wide agreement needed here, that's the point of the extension layer.

```jsonc
// domain: "engineering.v1"
{
  "repo": "org/service-name",
  "files_changed": ["src/api/auth.ts"],
  "pr_link": "https://github.com/org/repo/pull/123",
  "root_cause": "race condition in token refresh",
  "fix_type": "bugfix"
}

// domain: "support.v1"
{
  "ticket_id": "SUP-4821",
  "product_area": "billing",
  "resolution_category": "workaround",
  "csat_impact": "neutral"
}

// domain: "sales.v1"
{
  "account_id": "acct_tok_9f21",   // tokenized, never raw customer identifier
  "deal_stage": "negotiation",
  "objection_type": "pricing"
}

// domain: "data-ml.v1"
{
  "dataset_ref": "s3://.../dataset_v3",
  "model_name": "churn-predictor",
  "experiment_id": "exp-2026-08-05-01",
  "metric_deltas": {"auc": 0.02}
}
```

Where domain schemas live: a flat `schemas/domains/*.json` folder in the repo, one file per `domain` value, each with its own version suffix. A department adds a new file or bumps its version independently of the core schema.

### 4.0 `domain_data` validation mechanism (adopted from Glean's Indexing API)

Previously this schema said `domain_data` is "validated against that domain's own schema" without defining how. Adopted mechanism: each domain schema file declares a **type per field name**, and the Gate validates every `domain_data` value against its declared type at ingest — same approach Glean's Indexing API uses for its custom-metadata fields.

```jsonc
// schemas/domains/engineering.v1.json
{
  "domain": "engineering.v1",
  "fields": {
    "repo": "string",
    "files_changed": "string[]",
    "pr_link": "url",
    "root_cause": "string",
    "fix_type": "enum:bugfix|feature|refactor|hotfix"
  }
}
```

On mismatch (e.g. `fix_type: "typo-fix"` when the enum doesn't include it, or `pr_link` isn't a valid URL), the Gate rejects the record with an error identifying the offending field and value — mirrors Glean's Indexing API returning a 400 that names the specific document and value that violated its declared type. The record doesn't silently pass through with bad data; the author/agent gets a clear, immediate error to fix and resubmit.

### 4.1 Vocabulary serving — ISR-style caching

The canonical vocabulary + synonym map (used by Gate-side normalization in the parent architecture doc) is served to agents via the MCP tool schema, not queried live on every call:

- The server precomputes a cached snapshot of `{canonical terms, synonym map}` per vocabulary (`department`, `entity.type`).
- Agents get the cached snapshot instantly on tool-list/tool-call — no per-request DB lookup.
- The cache is **revalidated on a background interval** (or immediately after a human approves a pending term during vocabulary curation), not recomputed synchronously per request — the same stale-while-revalidate idea as ISR for a search index: fast reads, eventually-consistent writes.
- This means an agent might briefly suggest a term that was just merged as a synonym elsewhere; that's fine — the Gate still normalizes it correctly on ingest even if the agent's local suggestion list is a step behind.

## 5. Context bus event (compact projection, not the full record)

The bus carries a small **notification + pointer**, not the full record — keeps messages small and consumers fetch full detail from Gold on demand.

| Field | Type |
|---|---|
| `record_id` | uuid |
| `session_id` | string |
| `department` / `team` | string |
| `agent_id` | string \| null |
| `event_type` | `created` \| `updated` \| `completed` \| `handed_off` |
| `title` / `summary` | string |
| `status` | enum (same as envelope) |
| `visibility` | enum (same as envelope) |
| `timestamp` | timestamp |
| `gold_ref` | URI to the full record |

## 6. Open decisions (resolve with the team before building)

1. Final controlled vocabulary lists for `department`, `entities[].type`.
2. ~~Where domain schemas are validated~~ — resolved: runtime check at the Gate, typed per field (§4.0).
3. Versioning policy — does a new `domain.vN` require migrating old records, or do consumers just handle multiple versions?

## Future work (deliberately out of scope for the hackathon)

- **Canonical entity registry** — resolving `entities[].value` mentions to a linked registry record (person/system/team), rather than freeform strings. Considered after reviewing Glean's "extract and link" pattern; reverted from the active schema on 2026-08-07 because the registry itself isn't being built for the hackathon and an unused `ref_id` field is speculative bloat, not a real improvement yet. Revisit post-hackathon.

## Changelog

- `1.0.0` (draft) — initial schema defined: envelope + core content + domain extension pattern.
- `1.1.0` — adopted from Glean research (`glean-research.md`): typed, validated `domain_data` fields with ingest-time rejection (§4.0).
- `1.1.1` — reverted a speculative `entities[].ref_id` field added in 1.1.0 alongside the validated `domain_data` change; moved to Future Work instead (see above).
- `1.2.0` — added `consent_basis` and `consent_actor` to the envelope: every record must now be traceable to either an admin-mandated policy rule or explicit employee opt-in. Resolves the previously open "ingestion paths" question in `data-passport-architecture.md`.
