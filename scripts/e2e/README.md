# Layer 3 — live end-to-end acceptance

These are NOT pytest. They need the Context Bus actually running (Postgres on
5433 + `uvicorn app.main:app --port 8000`, see `store/docs/orgbrain-setup.md`,
with `.venv/bin/python` on Linux rather than the doc's Windows paths).

They drive `gateway.flows` against the real bus, seed their own data, assert,
and clean up after themselves by session-id prefix.

```bash
.venv/bin/python scripts/e2e/e2e_read.py    # G4  — retrieval + DLP + visibility
.venv/bin/python scripts/e2e/e2e_write.py   # G6  — the full demo, incl. stop-ship
```

`e2e_write.py` is the acceptance test for the demo narrative:

| # | Asserts |
|---|---|
| 1 | **stop-ship** — a captured draft puts ZERO rows in the DB |
| 2 | a credential in the draft is redacted, and flagged |
| 3 | `ESDS_APPROVE` writes exactly one row |
| 3b | the secret is absent from the row *and* from the Bronze file on disk |
| 4 | `redaction_audit_log` records WHO asserted the flags (store S3) |
| 5 | authorship comes from the bearer token, not the body (store S5) |
| 6 | `visibility: team` hides the record from another team |
| 7 | `--visibility org` at approval makes it visible to that team |

Layer 1 (no services needed) is `pytest gateway/tests`.
