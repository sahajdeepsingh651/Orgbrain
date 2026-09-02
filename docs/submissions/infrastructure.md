# What technology or infrastructure is needed?

Self-hosted except the outbound model API call.

- **Gateway** — Python / FastAPI, stateless, 3 replicas behind a load balancer, 2 vCPU +
  4 GB each.
- **Worker** — same image, separate process, scales on queue depth.
- **Database** — PostgreSQL 16 + pgvector 0.8+, HNSW index. Primary + read replica,
  PgBouncer, WAL archiving. 8 GB RAM — the index stays resident (100k records ≈ 1 GB).
- **Embeddings** — BGE-base-en-v1.5, 768-dim, CPU, in-process. No GPU.
- **Extraction** — Claude Opus 5 via the Anthropic API, structured outputs.
- **Secrets** — Vault or cloud KMS. Placeholder vault AES-GCM encrypted, 15-minute TTL.
- **Identity** — OIDC single sign-on, SCIM or LDAP sync from Active Directory.
- **Network** — TLS 1.3, database on a private subnet, egress allowlisted to
  `api.anthropic.com` only.
- **Observability** — Prometheus / Grafana, OpenTelemetry, JSON logs.
- **Deployment** — containers on Kubernetes or Compose, 120-second connection draining for
  in-flight streams.
- **Capacity** — single organization API key; size token-per-minute limits for aggregate
  peak, per-developer quotas in the gateway.

Not required: vector database, message broker, cache tier, GPU.
