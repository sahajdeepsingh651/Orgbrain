# Gateway — Component Overview

**Component:** `gateway/` — the Data Passport interception gateway
**Status:** working prototype; policies are test-grade, not production
**Audience:** anyone joining the project, reviewing it, or testing it
**Companion docs:** `ARCHITECTURE.md` (the design), `TEST-PLAN.md` (the validation
ladder that decided the design), `docs/QA-TEST-GUIDE.md` (how to test this),
`docs/WIRE-FINDINGS.md` (what real traffic actually looks like)

> This document describes **what exists in the code today**. `ARCHITECTURE.md`
> describes the intended system. Where they differ, this document is right about
> the present and `ARCHITECTURE.md` is right about the destination. The
> "Maturity" table below is the map between them.

---

## 1. What this component is

A reverse proxy that sits between an AI coding harness and the model API.

```
┌──────────────┐        ┌─────────────────────┐        ┌──────────────────┐
│ Claude Code  │  POST  │   Data Passport     │  POST  │ api.anthropic.com│
│ Cursor       │───────►│      Gateway        │───────►│                  │
│ Aider, SDK…  │        │  (this component)   │        │                  │
└──────────────┘◄───────└─────────────────────┘◄───────└──────────────────┘
       ▲          SSE            │      ▲          SSE
       │                         │      │
  ANTHROPIC_BASE_URL             ▼      │
  = http://gateway         CHECK / READ / WRITE
```

The developer changes **one environment variable** and nothing else:

```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

Their tool, commands, and workflow are unchanged. The tool does not know the
gateway exists.

## 2. Why a gateway and not a hook, skill, or MCP server

The binding constraint is **do not change the interface developers work in** —
anything requiring installation, invocation, or a habit gets used by the people
who already believe in it, and the brain is worthless at 10% participation.

| Mechanism | Changes the interface? | Fires reliably? | Works across harnesses? |
|---|---|---|---|
| Skill / `CLAUDE.md` | No | **No** — the model chooses | No |
| MCP tool | Per-tool config | **No** — the model chooses | No |
| Claude Code hook | No | Yes | **No** — one harness |
| **Base-URL redirect** | **No** | **Yes** | **Yes** |

Two properties only the gateway has:

1. **It sees the whole payload, not the prompt.** A `UserPromptSubmit` hook sees
   the sentence the developer typed. It never sees the config file the agent
   read three tool calls later — and that file is where credentials actually
   leak from. Confirmed in `docs/WIRE-FINDINGS.md`: a planted key lands inside a
   `tool_result` block and appears in no user-authored text.
2. **Two deployment tiers, one server.** Tier 1 is the env var — opt-in, trivial,
   and the developer can unset it. Tier 2 is network-level with a corporate CA,
   which is unbypassable and catches tools that expose no base-URL setting. Tier
   1 is the demo; tier 2 is what makes the DLP claim an enforcement boundary
   rather than a feature.

**Honest boundary:** "any harness" means any harness that runs locally *and*
exposes an endpoint setting. Browser sessions on claude.ai or chatgpt.com cannot
be redirected at all.

---

## 3. Request pipeline

```
POST /{path}
   │
   ├─ detect()                    tier 1: path /v1/messages · tier 4: model "claude-*"
   │     └─ None  ──────────────► passthrough_raw()  byte-identical, no mutation
   │
   ├─ adapter.to_normalized()     wire JSON → NormalizedRequest
   │                              (also parses session_id / account_uuid — G1)
   │
   ├─ CHECK  check.scan()         redact sk-test-… → ⟦SECRET_n⟧
   │         pii.scan()           redact PII       → ⟦PII_n⟧
   │                              both mint per UNIQUE VALUE, into one vault
   │
   ├─ READ   flows.handle_read()  ESDS_SEARCH only, in the last genuine human turn
   │           ├─ identity.resolve(account_uuid)   unknown ⇒ FAIL CLOSED, no bus call
   │           ├─ GET /v1/search                   bus enforces visibility
   │           ├─ scan_text() each document        ◄── retrieved content is DLP-scanned
   │           └─ add_context(rendered)                BEFORE it is injected
   │
   ├─ WRITE  flows.handle_write_request()
   │           ├─ drain queued writes for this session   (G8)
   │           ├─ ESDS_APPROVE ⇒ POST /v1/ingest   ◄── the only bus write in the codebase
   │           ├─ ESDS_REJECT  ⇒ discard pending
   │           └─ ESDS_SUBMIT  ⇒ mint pending_id, inject extraction instruction
   │
   ├─ AWARENESS flows.handle_awareness()   only if no marker fired; titles only
   │
   ├─ adapter.from_normalized()   NormalizedRequest → wire JSON
   │                              (re-emits the ORIGINAL message list untouched
   │                               when no policy mutated it — GT fidelity)
   │
   ├─ forward ──────────────────► upstream
   │
   ├─ non-streaming:  parse_response_json → restore → JSONResponse
   └─ streaming:      relay()
           ├─ tee(aiter_raw)  ──► raw bytes into parse_buf   ◄── never the restored ones
           ├─ _restore_sse_stream → yield to client immediately
           └─ after the stream: log usage, capture any WRITE draft
```

Two orderings here are load-bearing and neither is obvious:

1. **Retrieved documents are scanned specifically, not by moving the global CHECK.**
   Moving CHECK below READ would re-scan the whole conversation every turn and
   double-redact already-tokenized text. Instead `flows._scan_hits()` scans the
   retrieved records into the *same* vault, so the response restorer covers them.
2. **The streaming side buffer tees raw bytes.** `parse_buf` feeds the usage log and
   the WRITE draft extractor. When it accumulated from the *restored* stream, an
   approved draft would have carried real secret values into the Context Bus.

## 4. Module map

```
gateway/
  app.py                 the pipeline. transport + orchestration only.
  flows.py               READ / AWARENESS / WRITE orchestration. The ONLY place
                         that does bus I/O. `bus` is injectable for tests.
  bus_client.py          REST client for the Context Bus. Not MCP — that server is
                         stdio, one identity per process, wrong shape for a proxy.
  pending.py             drafts awaiting human approval. Nothing here can write to
                         the bus; only the approval path can.
  failure.py             the failure policy as a TABLE (Flow × Failure → Disposition),
                         exhaustive by test.
  protocol/
    normalized.py        NormalizedRequest/Message/Response. `extra` round-trips
                         every key the adapter doesn't model; `metadata` is
                         gateway-internal and never reaches the wire.
    detect.py            which adapter, or None (⇒ fail open)
    anthropic_adapter.py the ONLY wire-format-aware code
  policies/
    check.py             test-secret detector + restore() + StreamRestorer
    pii.py               the real detector suite (regex + checksums + JSON fields)
    read.py              pure: human-turn predicate, renderers, relevance floors
    markers.py           positional marker authorization (all four markers)
    identity.py          account_uuid → bus identity. Unknown ⇒ None ⇒ fail closed.
    write.py             pure: extraction instruction, draft parsing, validation,
                         sensitivity_flags derivation
  tests/                 141 tests, no services required
```

**A wire-format string appearing in `gateway/policies/` is a defect.** The policies
operate on `NormalizedRequest` only; `anthropic_adapter.py` owns every Anthropic-ism.
`flows.py` is the deliberate exception for I/O, not for wire formats.

## 5. The policies

### CHECK — the border (`policies/check.py`, `policies/pii.py`)

Scans **every** request unconditionally. Two suites share one vault on disjoint
prefixes (`⟦SECRET_n⟧` / `⟦PII_n⟧`) so a single `StreamRestorer` restores both.

Tokenise-and-restore, not `[REDACTED]`. Four rules: preserve type, preserve
coreference (same value ⇒ same token, always), preserve the semantically-loaded
part, restore on the way back. A model can still reason about "the key" without
ever seeing it, because a secret is random and random carries no meaning to
reason from.

`pii.py` covers email, PAN (holder-type gated), GSTIN (mod-36), IFSC, card
(Luhn), Aadhaar (Verhoeff), ISO dates, self-introduced names, phone numbers via
`phonenumbers`, plus a JSON field-name pass. It walks text blocks, nested
`tool_result` content, **and `tool_use.input`** — outbound tool arguments are a
third payload shape, and they leaked before that branch existed.

Reach is now identical across both suites: `check.py` was backported with the
same recursion and the same per-unique-value dedup ledger.

### READ — retrieval and awareness (`policies/read.py`, `flows.py`)

Two distinct operations, deliberately separated:

| | Awareness | Retrieval |
|---|---|---|
| Trigger | automatic, genuine human turn | explicit `ESDS_SEARCH` |
| Payload | titles + count | full records |
| Distance ceiling | 0.62 | 1.0 |
| Timeout | 300 ms | 3 s |
| On failure | silent | logged |

The floors differ on purpose. Under an explicit search the human asked, sees the
results, and can retype — a miss is recoverable. Awareness fires unprompted and
cannot be corrected, so it must be stingy. Zero results is a correct answer;
injecting nothing is right.

### WRITE — draft, validate, **approve**, ingest (`policies/write.py`, `pending.py`)

```
ESDS_SUBMIT (human turn)  →  mint pending_id, inject extraction instruction
model replies             →  fenced ```json block, streamed VISIBLY to the user
gateway                   →  validate against /v1/ingest's contract
                          →  DLP-scan → derive sensitivity_flags
                          →  park in pending.  NOTHING IS SENT.
ESDS_APPROVE <id>         →  POST /v1/ingest with an Idempotency-Key
```

**Validation is not approval.** A schema-valid, DLP-clean, correctly-attributed
draft still stops at the pending store. There is exactly one `bus.ingest()` call
in the codebase and it is reachable only from a human's own `ESDS_APPROVE`.

The pending id is minted **before** the model replies and the model is told to
print it. A proxy cannot push; revealing the id on the next request would make a
write take three turns, the middle one existing only to be told what to type.

Fields the model may not choose: `session_id` and identity come from the adapter
and the bearer token; `visibility` defaults to `team` and only the human may
widen it at approval time. `--visibility orgg` falls back to `team`, never `org`.

## 6. The injection point, and the model-gating fallback

This is the load-bearing decision in the read path.

Rewriting the top-level `system` field would change the very front of the prompt
prefix and **invalidate prompt caching for the entire conversation** — every turn
re-billed at full price. So injection always goes at the **tail of `messages[]`**,
after the cached prefix.

Anthropic supports a mid-conversation `{"role": "system", …}` message — the
non-spoofable operator channel — but **only on some models**:

```python
_SUPPORTS_MIDCONV_SYSTEM_ROLE = (
    "claude-opus-5", "claude-opus-4-8", "claude-fable-5", "claude-mythos-5",
)
```

`claude-sonnet-5` is **not** on that list, and `docs/WIRE-FINDINGS.md` confirms
Claude Code on this machine sends `claude-sonnet-5`. So on real traffic the
adapter takes the fallback path: it folds the content into the preceding user
turn as a `<system-reminder>` block.

```
supported model  →  {"role": "system",  "content": [{"type":"text","text":"…"}]}
other models     →  {"role": "user", "content": [ …original…,
                        {"type":"text","text":"<system-reminder>\n…\n</system-reminder>"} ]}
```

Same position, same cache cost, **lower trust** (user-turn text is in principle
spoofable; an operator-role message is not).

**This is the single most common source of "the test failed" reports that are
not bugs.** On Sonnet you should expect `<system-reminder>`, not `role: "system"`.

Note also that the policy layer never learns any of this. `read.py` says "append
authoritative context"; the adapter decides what that means on this wire for this
model. That separation is the point of the normalized layer — the same primitive
serves WRITE's extraction trigger for free.

---

## 7. Operational contract

### Running it

```bash
# the Context Bus must be up first — see store/docs/data-passport-setup.md
.venv/bin/python -m uvicorn gateway.app:app --port 8080

# separate terminal — NOT exported
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

> **Never `export ANTHROPIC_BASE_URL`** into a shell you also use for other Claude
> Code work — it redirects everything that shell does for as long as it is set.

### Auth: relay mode

The gateway holds **no credential of its own** for the upstream — it forwards the
client's `Authorization` / `x-api-key` headers unmodified. It *does* hold a bus
token per developer, resolved from `account_uuid` via `store/config/account_map.json`.
An unknown account resolves to `None`, and every bus-touching path treats `None` as
fail-closed. Never invent a default token: that hands one user another's records.

### Environment variables

| Var | Default | Effect |
|---|---|---|
| `DP_UPSTREAM_BASE_URL` | `https://api.anthropic.com` | upstream override (testing) |
| `DP_CHECK_RESTORE_STREAM` | **`1`** | streaming restoration. `0` gives the old byte-identical relay for a fidelity measurement. No-op when nothing was redacted |
| `DP_INJECT` / `DP_INJECT_TEXT` | `0` | legacy injection scaffolding, kept alive for T2/T4 |
| `DP_BUS_BASE_URL` | `http://127.0.0.1:8000` | Context Bus |
| `DP_BUS_TIMEOUT` / `DP_BUS_INGEST_TIMEOUT` | `3.0` / `10.0` | per-call timeouts |
| `DP_IDENTITY_MAP` | `store/config/account_map.json` | account → bus identity |
| `DP_SEARCH_LIMIT` / `DP_SEARCH_MAX_DISTANCE` | `5` / `1.0` | retrieval |
| `DP_AWARENESS` | `0` | enable the awareness probe |
| `DP_AWARENESS_TIMEOUT` / `_COOLDOWN` / `_LIMIT` / `_MAX_DISTANCE` | `0.3` / `300` / `3` / `0.62` | awareness tuning |
| `DP_PENDING_DIR` | `/tmp/dp_pending` | drafts awaiting approval |
| `DP_WRITE_MAX_RETRIES` | `2` | bounded side-call retries for an invalid draft |
| `DP_DRAIN_ON_REQUEST` | `1` | retry writes queued while the bus was down |
| `DP_DEBUG_LOG_OUTBOUND` | `0` | **test-only** — writes outbound payloads to `/tmp` in plaintext |
| `DP_ARM_LABEL` | `""` | tag in each usage-log line |

`DP_WRITE_TEST` is **gone**. G6 gates the extraction instruction on `ESDS_SUBMIT`
in a genuine human turn instead of an env flag.

### Artefacts written

| Path | What |
|---|---|
| `docs/usage_log.jsonl` | one line per response: model, usage, whether injected |
| `/tmp/dp_pending/*.json` | drafts awaiting approval. **Nothing here has reached the bus** |
| `/tmp/dp_outbound_debug_*.json` | only when `DP_DEBUG_LOG_OUTBOUND=1` |
| `fixtures/*.json` | captured request bodies — **gitignored, may hold real secrets** |

### Invariants that must not regress

1. **Fail open** on unrecognised protocol, non-JSON body, or bus failure — forward
   raw bytes untouched. **Fail closed** on DLP failure and unknown identity.
2. **Never whole-response-buffer.** `yield chunk` inside the `async for`.
3. **The side buffer holds raw bytes**, never restored ones.
4. **Markers are honoured by position**, never by presence.
5. **No approval, no ingest.** One `bus.ingest()` call, reachable only from a human.
6. **Never report success for a write that did not happen.** Queue and say so.
7. The upstream key must never reach a log line.

## 8. Maturity — what is real, what is scaffolding

| Area | State |
|---|---|
| Base-URL interception, non-buffered SSE relay | **working** |
| protocol detect → normalized → adapter | **working** |
| Model-gated injection + `<system-reminder>` fallback | **working** |
| Session / account identity from request metadata | **working** |
| Positional marker authorization (4 markers) | **working** |
| PII + secret detection, tokenise-and-restore, streaming restore | **working** |
| Retrieval behind `ESDS_SEARCH`, with retrieved-content DLP | **working** |
| Awareness probe | **working**, off by default |
| Write path with human approval gate + idempotency | **working** |
| Failure table + queued-write drain | **working** |
| Contradiction detection | **not built** — the schema hook exists (`links[].type` has `contradicts`/`supersedes`) |
| Dashboard / admin UI | **not built** — belongs on the store |
| OpenAI adapter | **not built** — `detect.py` tiers 2–3 are deliberate stubs |
| `dp_*` key issuance | **not built** — `account_map.json` is the stand-in |
| Tier-2 (network-level + corporate CA) deployment | **not built** |

### Known issues and open questions

1. **`detect.py` matches `startswith("/v1/messages")`**, so `/v1/messages/count_tokens`
   is also normalized and policy-mutated — token counts would include injected context.
2. **The invariant is enforced by omission.** The harness executes MCP tool calls
   locally; they never reach the model API, so the gateway is structurally blind to
   them. The honest claim is "nothing confidential leaves through the model API".
3. **The bus computes embeddings synchronously inside async handlers**
   (`store/backend/app/serving.py:110`), so every `/v1/search` blocks its event loop.
   The awareness probe issues one per human turn per developer — keep the timeout and
   cooldown as they are.
4. **HNSW-before-visibility gap** in `/v1/search`: a permitted record can be silently
   absent. Documented and accepted upstream; do not "fix" with an unindexed scan.
5. **`"name"` was removed from the PII JSON field list** to stop it redacting tool
   metadata like `{"name": "create_ticket"}`. Consequence: a genuine
   `{"name": "Rohan Mehta"}` in a `tool_result` is now caught only if the free-text
   name regex fires.
6. **Queued writes for a session that never returns** stay queued until someone runs
   `scripts/drain_queue.py`. The opportunistic drain only covers returning sessions.

## 9. Where to start reading

1. `gateway/app.py` — the whole pipeline in one function.
2. `gateway/flows.py` — the three flows, and the ordering rule stated once.
3. `gateway/policies/markers.py` — the security predicate everything else reuses.
4. `TESTING.md` — how to run any of it.
5. `docs/QA-TEST-GUIDE.md` — the tester-facing case list.
