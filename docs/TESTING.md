# How to test Data Passport

Three layers. Layer 1 needs nothing running. Layer 3 is the demo, as a test.

---

## Layer 1 — unit (no services, ~0.5s)

```bash
cd ~/Projects/hackathon_agent_layer
.venv/bin/python -m pytest gateway/tests -q          # 141 tests
```

This is the one to run after every change. It covers the security boundary, so
**it must never go from green to red**:

| File | Guards |
|---|---|
| `test_g2_markers.py` | marker authorization — a marker in a `tool_result` must never be honoured |
| `test_g4_retrieval.py` | retrieved bus content is DLP-scanned **before** injection |
| `test_g6_write.py` | **the stop-ship test** — no approval means no ingest |
| `test_g7_streaming.py` | the side buffer tees raw bytes, so a draft can't capture real secrets |
| `test_g8_failures.py` | the failure table is exhaustive and matches observed behaviour |
| `test_gp_pii_fixes.py` | the four PII detector fixes |
| `test_gt_passthrough.py` | passthrough fidelity |
| `test_g1_identity.py` | session/account extraction from real fixture bodies |

Run one file: `.venv/bin/python -m pytest gateway/tests/test_g6_write.py -q`

**The single most important test.** If you only run one thing:

```bash
.venv/bin/python -m pytest gateway/tests -q -k STOPSHIP -v
```

A draft that was never approved must produce **zero** calls to `/v1/ingest`.
If that fails, the AI can write to organisational memory on its own and the
central claim of the project is false.

---

## Layer 2 — gateway against a stub upstream (no API spend, no Docker)

Two terminals. Nothing here costs Anthropic credit.

```bash
# terminal 1 — fake Anthropic
.venv/bin/python -m uvicorn scripts.stub_upstream:app --port 9090

# terminal 2 — the gateway, pointed at it
DP_UPSTREAM_BASE_URL=http://127.0.0.1:9090 \
  .venv/bin/python -m uvicorn gateway.app:app --port 8080
```

Then drive it with `curl` (see `docs/QA-TEST-GUIDE.md` for the full case list).

Stub modes — set on terminal 1:

| `DP_STUB_MODE` | Reply |
|---|---|
| `echo` (default) | `ECHO>> [user] ...` — what every existing QA case expects |
| `verbatim` | the last human text block, unprefixed, so a marker lands at line start |
| `draft` | a canned fenced-JSON knowledge draft, for exercising the write path |
| `fixed` | whatever `DP_STUB_REPLY` says |

Other knobs: `DP_STUB_STATUS=500` forces an error (drives the failure table),
`DP_STUB_CHUNK` / `DP_STUB_DELAY` control SSE chunking (default 4 bytes / 0.3s,
chosen so redaction tokens split across chunk boundaries — set `DP_STUB_DELAY=0`
for fast runs).

---

## Layer 3 — live, against the real Context Bus

### Bring the bus up

The store's own setup doc was written on Windows; on Linux the venv path is
`.venv/bin/python`, not `.venv/Scripts/python.exe`.

```bash
cd store
docker compose up -d                       # Postgres + pgvector on port 5433
cd backend
set -a; source ../.env; set +a
.venv/bin/python db/migrate.py             # idempotent
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Do **not** add `--reload` — the watcher can log `Reloading...` without actually
respawning, and you end up testing stale code.

Confirm: `curl -s http://127.0.0.1:8000/openapi.json | head -c 80`

### The store's own suite

These are plain scripts, **not pytest**. They need Postgres *and* the server up.

```bash
cd store/backend
.venv/bin/python tests/test_ingest.py
.venv/bin/python tests/test_serving.py
.venv/bin/python tests/test_context_bus.py
.venv/bin/python tests/test_idempotency_race.py    # concurrent Idempotency-Key handling
```

> ⚠️ `tests/test_ingest.py` cleans up with `DELETE FROM redaction_audit_log WHERE
> session_id LIKE 'sess-ingest-%' OR session_id IS NULL`. That `OR` wipes **every**
> NULL-session audit row, including ones the gateway wrote. Don't run it against a
> database holding demo data you care about.

### The acceptance tests — the demo, automated

```bash
cd ~/Projects/hackathon_agent_layer
.venv/bin/python scripts/e2e/e2e_read.py     # retrieval + DLP + visibility
.venv/bin/python scripts/e2e/e2e_write.py    # the full demo, incl. stop-ship
```

`e2e_write.py` is the one to run before demoing. It asserts:

| # | Claim |
|---|---|
| 1 | **stop-ship** — a captured draft puts ZERO rows in the database |
| 2 | a credential in the draft is redacted, and flagged |
| 3 | `ESDS_APPROVE` writes exactly one row |
| 3b | the secret is absent from the row **and** from the Bronze file on disk |
| 4 | `redaction_audit_log` records *who asserted* the flags |
| 5 | authorship comes from the bearer token, not the request body |
| 6 | `visibility: team` hides the record from another team |
| 7 | `--visibility org` at approval makes it visible to that team |

Both scripts seed their own data and clean up by session-id prefix.

---

## Manual — a real Claude Code session

This spends real API credit. Do it once, before the demo, not in a loop.

```bash
# terminal 1: bus (as above)
# terminal 2: gateway
.venv/bin/python -m uvicorn gateway.app:app --port 8080

# terminal 3: a real session through it
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

> ⚠️ **Never `export ANTHROPIC_BASE_URL`** in a shell you also use for other
> Claude Code work — it redirects everything that shell does, for as long as it
> is set. Always prefix it onto the single command.

Then, in that session:

1. Ask a normal question. → nothing happens; the gateway is invisible.
2. Type `ESDS_SEARCH kafka retries` → retrieved records appear in the answer.
3. Type `ESDS_SUBMIT` → the model drafts a record and prints
   `To save this, type ESDS_APPROVE <id>`.
4. **Check the bus is still empty** — this is the demo's best beat:
   ```bash
   ls /tmp/dp_pending/          # the draft is here
   curl -s -H "Authorization: Bearer dev-local-token" \
     "http://127.0.0.1:8000/v1/search?q=<your topic>" | head -c 200   # nothing
   ```
5. Type `ESDS_APPROVE <id>` → now it's saved. Re-run the curl; it's there.
6. Paste a fake key (`sk-test-abcdefghij123`) and prove it never leaves:
   ```bash
   DP_DEBUG_LOG_OUTBOUND=1   # on the gateway, then grep /tmp/dp_outbound_debug_*.json
   ```

To enable the awareness probe (off by default), start the gateway with
`DP_AWARENESS=1`.

---

## Quick reference

| Want to check | Command |
|---|---|
| Nothing is broken | `pytest gateway/tests -q` |
| The core claim holds | `pytest gateway/tests -q -k STOPSHIP` |
| The bus is healthy | `curl -s localhost:8000/openapi.json \| head -c 80` |
| The demo works | `python scripts/e2e/e2e_write.py` |
| Queued writes are stuck | `python scripts/drain_queue.py` |
| What's pending approval | `ls /tmp/dp_pending/` |

**Never put a real credential in a test.** Use the fake `sk-test-…` shape only.
`fixtures/*.json` are gitignored because they hold real captured request bodies —
never commit them or paste them into a ticket.
