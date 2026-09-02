# Glean — Research Notes & What's Useful for Data Passport

> Purpose: understand how a mature enterprise search/Work AI product (Glean, $7.2B) solves an adjacent problem, and pull out concrete ideas worth adopting. Not a spec — recommendations below need team confirmation before they change `data-passport-schema.md` or `data-passport-architecture.md`.

## 1. What Glean is

Enterprise search + AI assistant platform (founded 2019, ex-Google Search engineers). Connects to a company's existing tools (Slack, GitHub, Jira, Drive, Confluence, etc.), indexes everything, and answers questions across all of it — while strictly enforcing who's allowed to see what.

## 2. How it's architected

**Connectors** — one per source system. Initial full crawl, then incremental updates via webhooks/periodic diffs. Each connector pulls three things together at once: content, metadata, and the source system's permission map (ACL). Permissions are never separated from content.

**Identity resolution** — aligns a person's different usernames/emails across tools into one identity, so permissions and activity attribute consistently.

**Permission-aware retrieval** — permissions are the *first* filter at query time, not a post-hoc scrub after retrieval. Their own materials call this the difference between "deployable in a regulated enterprise" and "dangerous" (naive RAG searches everything, then filters — which still leaks via timing/relevance signals).

**Enterprise Graph** — structured entities (people, teams, docs, tickets, repos, customers, accounts, projects) and the relationships between them (ownership, collaboration, dependency), so multi-hop questions ("what's blocking the launch?") can be answered by traversing relationships, not just matching one document.

**Storage split (hybrid, not one database):**
- Structured entities/relationships → Knowledge Graph
- Unstructured semantic content → Vector embeddings (trained via SageMaker/Vertex AI depending on cloud, served through search index + Amazon RDS / BigQuery depending on deployment)
- Lexical search index (OpenSearch) alongside the vector index for code/text search
- Ranking combines vector similarity + graph-derived signals + activity signals (views, edits, shares) — hybrid retrieval, not vector-only

**Ingestion has three distinct paths, not one:**
1. **Indexing** — unstructured content (docs, wikis, emails), crawled and stored for high-recall/low-latency retrieval.
2. **Tools** — direct integrations triggering workflows or fetching dynamic/live data at query time.
3. **Context Provisioning** — programmatic context injected into a session **without indexing it** — used when content should inform the current interaction but shouldn't become a permanent record.

**Tenancy/security** — single-tenant infrastructure; connectors and all data processing run *inside the customer's own cloud project*; data is stated to never leave the tenant's environment. Encrypted in transit and at rest.

## 3. Document schema (from the Indexing/Push API)

```
Document {
  datasource: string          // which connector/source this came from
  objectType: string          // e.g. "Document"
  id: string                  // unique id
  title: string
  body: {
    mimeType: string          // e.g. "text/plain"
    textContent: string
  }
  viewURL: string              // link back to the original content
  permissions: {
    allowAnonymousAccess: boolean
    allowedUsers: [{ email }]
  }
}

Datasource {
  name, displayName
  datasourceCategory          // e.g. PUBLISHED_CONTENT
  urlRegex
  isUserReferencedByEmail
}

CustomMetadata (schema group, declared per datasource):
  metadataKeys: [{ name, propertyType }]   // propertyType e.g. PICKLIST, MULTIPICKLIST
  customMetadata (on a document): [{ name, value }]
```

Key mechanism: **custom metadata fields declare a type up front**, and the API validates every value against that declared type at ingest — a document with a mismatched value gets a 400 error identifying exactly which doc and value failed. Type enforcement happens at the write boundary, not discovered later at query time.

## 4. What's useful for Data Passport — recommendations

| Idea from Glean | Where it'd apply in our design | Status |
|---|---|---|
| Typed, declared custom-metadata fields validated at ingest (400 on mismatch) | Our `domain_data` extension payload (`data-passport-schema.md` §4) currently says "validated against that domain's own schema" with no defined mechanism. Adopt: each domain schema file declares a type per field name; the Gate validates and rejects with a clear error, same as Glean's 400. | Proposed — needs team confirmation |
| Entity extraction *and linking* to a canonical registry, not just freeform tags | Our `entities[]` field (`{type, value}`) is freeform today. Consider resolving mentions against an entity registry at extraction time — same underlying problem as our department/entity-type vocabulary-drift discussion, just applied one level deeper (specific people/systems, not just categories). | Proposed — worth deciding alongside the vocabulary Gate design |
| Three distinct ingestion paths (Indexed / Live-Tool / Context-only-no-index) | We currently only have one path: everything captured becomes a Silver/Gold record. Worth a real decision: should some session content flow through as context for the moment without ever becoming a permanent knowledge record? | Open question — not yet decided |
| Data never leaves the tenant boundary (processing runs inside customer's own cloud project) | Direct external validation for our Egress Gate's on-prem-vs-external policy (`data-passport-security-egress.md`) — a $7.2B enterprise vendor's whole regulated-industry pitch rests on the same boundary logic we're already proposing. | Confirms existing design, no change needed |
| Hybrid retrieval: vector similarity + graph-relationship signals + activity signals, not vector-only | Our Gold layer today is vector-only (pgvector). We already have a `links[]` field (continues_from, supersedes, related_to, etc.) in the envelope — could feed that into ranking as a relationship signal without needing a separate graph database. | Proposed — lightweight version achievable with what we already planned |
| Permissions as explicit ACL (`allowAnonymousAccess`, `allowedUsers[]`) rather than a coarse visibility enum | Our `visibility` field is a 4-value enum (`private/team/department/org`). Simpler to build, but worth knowing the mature-product version is per-document explicit ACLs mirrored from source permissions. | Note only — enum is the right call for hackathon scope, ACL is the "if we had more time" answer |

## 5. What we're intentionally *not* copying

- Separate dedicated knowledge-graph database — out of scope for a hackathon; our `links[]` field + Postgres relations gets most of the value without a new service.
- Per-cloud custom embedding model training (SageMaker/Vertex AI) — way beyond hackathon scope; a standard embedding API call into pgvector is the right substitute.
- Full ACL mirroring from every source system — our visibility enum is deliberately coarser and faster to build.

## Sources

- [Enterprise AI System Design: Inside the AI Architecture of Atlassian & Glean](https://medium.com/@kevinrt6911/enterprise-ai-system-design-inside-the-ai-architecture-of-atlassian-glean-a36d80f5bc7f)
- [Enhancing AI security with permissions-aware frameworks](https://www.glean.com/perspectives/security-permissions-aware-ai)
- [What Glean's Knowledge Graph Approach Reveals About Enterprise AI Search](https://rmax.ai/notes/enterprise-ai-agents-knowledge-layer-beyond-rag/)
- [How connectors power the Glean experience](https://docs.glean.com/connectors/connectors-power-glean)
- [How Glean Code Search Works](https://docs.glean.com/security/how-code-search-works)
- [Glean Indexing API Overview](https://developers.glean.com/api-info/indexing/getting-started/overview)
- [Glean Data Flow Architecture](https://docs.glean.com/security/architecture/data-flow)
- [Knowledge graph vs vector database: how to choose your AI foundation](https://www.glean.com/blog/knowledge-graph-vs-vector-database)
- [Glean uses BigQuery and Google AI to enhance enterprise search | Google Cloud Blog](https://cloud.google.com/blog/products/data-analytics/glean-uses-bigquery-and-google-ai-to-enhance-enterprise-search)
