-- Data Passport — Core Service Postgres Schema
-- Matches data-passport-schema.md (envelope/core content/extension) and
-- data-passport-core-service.md §4 (Context Bus) and §7 build step 1.
-- Safe to re-run: every statement is idempotent.

CREATE EXTENSION IF NOT EXISTS vector;

-- Silver: one row per extracted knowledge record (envelope + core content + domain extension).
CREATE TABLE IF NOT EXISTS knowledge_entries (
    record_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Envelope (data-passport-schema.md §2.A)
    schema_version TEXT NOT NULL DEFAULT '1.2.0',
    session_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    department TEXT NOT NULL,
    team TEXT,
    author_user_id TEXT NOT NULL,
    author_role TEXT,
    agent_id TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    visibility TEXT NOT NULL
        CHECK (visibility IN ('private', 'team', 'department', 'org')),
    consent_basis TEXT NOT NULL
        CHECK (consent_basis IN ('admin_mandated', 'user_opted_in')),
    consent_actor_type TEXT NOT NULL
        CHECK (consent_actor_type IN ('policy_rule', 'user')),
    consent_actor_id TEXT NOT NULL,
    sensitivity_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL
        CHECK (status IN ('in_progress', 'completed', 'blocked', 'handed_off', 'abandoned')),
    links JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Core content (data-passport-schema.md §2.B)
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    intent TEXT,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('decision_made', 'insight_found', 'issue_resolved', 'blocker_hit', 'question_open', 'in_progress')),
    outcome_detail TEXT,
    key_points JSONB NOT NULL DEFAULT '[]'::jsonb,
    next_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    open_questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    entities JSONB NOT NULL DEFAULT '[]'::jsonb,
    artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    review_status TEXT NOT NULL DEFAULT 'auto_extracted'
        CHECK (review_status IN ('auto_extracted', 'human_verified')),

    -- Extension (data-passport-schema.md §2.C) — validated at the Gate against schemas/domains/*.json, not here.
    domain TEXT,
    domain_data JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS knowledge_entries_department_idx ON knowledge_entries (department);
CREATE INDEX IF NOT EXISTS knowledge_entries_team_idx ON knowledge_entries (team);
CREATE INDEX IF NOT EXISTS knowledge_entries_visibility_idx ON knowledge_entries (visibility);
CREATE INDEX IF NOT EXISTS knowledge_entries_agent_id_idx ON knowledge_entries (agent_id);
CREATE INDEX IF NOT EXISTS knowledge_entries_session_id_idx ON knowledge_entries (session_id);
CREATE INDEX IF NOT EXISTS knowledge_entries_created_at_idx ON knowledge_entries (created_at DESC);

-- Audit trail from endpoint-reported sensitivity_flags metadata, plus Gate validation failures.
-- Never stores raw PII/secret values — see data-passport-architecture.md § The Endpoint Checkpoint.
CREATE TABLE IF NOT EXISTS redaction_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id UUID REFERENCES knowledge_entries (record_id) ON DELETE SET NULL,
    quarantine_id UUID,
    session_id TEXT,
    source_system TEXT,
    sensitivity_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_failure JSONB,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('committed', 'quarantined')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- S3 — attribute sensitivity flags to their asserter. Add resolved token
-- identity + source_system to redaction_audit_log so the audit trail reads
-- "u-dev on Engineering asserted no PII" instead of an unattributed "no
-- PII" rumour. NOT a re-scan: the store never receives raw PII and must
-- NOT verify; this just records WHO claimed what (see decisions-log.md:23
-- for the standing "no server-side PII re-scanning" decision this honours).
ALTER TABLE redaction_audit_log ADD COLUMN IF NOT EXISTS asserted_by_user_id TEXT;
ALTER TABLE redaction_audit_log ADD COLUMN IF NOT EXISTS asserted_by_department TEXT;
ALTER TABLE redaction_audit_log ADD COLUMN IF NOT EXISTS asserted_by_team TEXT;
ALTER TABLE redaction_audit_log ADD COLUMN IF NOT EXISTS asserted_source_system TEXT;

-- S2 — ingest idempotency. A retry after a timeout duplicates the
-- passport AND the Bronze file (write_bronze runs first unconditionally).
-- A nullable UNIQUE on idempotency_key lets /v1/ingest return the
-- existing record_id with 200 on conflict. PG's NULL-in-UNIQUE accepts
-- multiple NULLs, so records sent without the header still insert fine.
ALTER TABLE knowledge_entries ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_entries_idempotency_key_idx
    ON knowledge_entries (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS redaction_audit_log_record_id_idx ON redaction_audit_log (record_id);
CREATE INDEX IF NOT EXISTS redaction_audit_log_outcome_idx ON redaction_audit_log (outcome);
CREATE INDEX IF NOT EXISTS redaction_audit_log_created_at_idx ON redaction_audit_log (created_at DESC);

-- Gold: current activity per agent ("what it's working on right now, and what it left off with").
-- One row per agent_id — history of past activity lives in knowledge_entries, not here.
CREATE TABLE IF NOT EXISTS agent_activity (
    agent_id TEXT PRIMARY KEY,
    user_id TEXT,
    team TEXT,
    department TEXT,
    project TEXT,
    session_id TEXT,
    task TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('in_progress', 'completed', 'blocked', 'handed_off', 'abandoned')),
    record_id UUID REFERENCES knowledge_entries (record_id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_activity_team_idx ON agent_activity (team);
CREATE INDEX IF NOT EXISTS agent_activity_department_idx ON agent_activity (department);
CREATE INDEX IF NOT EXISTS agent_activity_project_idx ON agent_activity (project);
CREATE INDEX IF NOT EXISTS agent_activity_updated_at_idx ON agent_activity (updated_at DESC);

-- Context Bus durable log (data-passport-core-service.md §4) — compact projection per
-- data-passport-schema.md §5, one row per commit. Append-only; NOTIFY fires on the same write.
CREATE TABLE IF NOT EXISTS context_bus_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id UUID NOT NULL,
    session_id TEXT NOT NULL,
    department TEXT NOT NULL,
    team TEXT,
    agent_id TEXT,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('created', 'updated', 'completed', 'handed_off')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    visibility TEXT NOT NULL,
    gold_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Supports the replay/catch-up query: WHERE created_at > last_seen.
CREATE INDEX IF NOT EXISTS context_bus_events_created_at_idx ON context_bus_events (created_at);
CREATE INDEX IF NOT EXISTS context_bus_events_department_idx ON context_bus_events (department);
CREATE INDEX IF NOT EXISTS context_bus_events_team_idx ON context_bus_events (team);
CREATE INDEX IF NOT EXISTS context_bus_events_visibility_idx ON context_bus_events (visibility);

-- Gold: one embedding per knowledge_entries row. HNSW index per decisions-log.md (2026-08-07) —
-- access-control filtering happens on the ANN candidate set, not before it; known/accepted gap,
-- do not "fix" by switching to an unindexed exact scan (see docs/decisions-log.md).
CREATE TABLE IF NOT EXISTS knowledge_embeddings (
    record_id UUID PRIMARY KEY REFERENCES knowledge_entries (record_id) ON DELETE CASCADE,
    embedding vector(384) NOT NULL,
    embedding_model TEXT NOT NULL DEFAULT 'BAAI/bge-small-en-v1.5',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS knowledge_embeddings_hnsw_idx
    ON knowledge_embeddings USING hnsw (embedding vector_cosine_ops);
