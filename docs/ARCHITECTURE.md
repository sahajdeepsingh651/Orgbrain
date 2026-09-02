# Data Passport — System Architecture

Status: draft, 2026-08-07. Layered: production target, with the 3-day demo cut marked
inline as **[CUT]** / **[KEEP]**.

Design constraint that selects every mechanism below: **do not change the interface
developers work in.** No new tool, no new commands, no per-harness plugin.

---

## 0. The three services

Sahaj's doc describes one of these. The other two are where most of the risk lives.

| Service | Responsibility | Sync? |
|---|---|---|
| **Gateway** | Sits in front of the LLM API. Intercepts, DLP-gates, retrieves, injects, tees the stream. | Synchronous, on the critical path of every call |
| **Context Bus** | Passport storage, semantic search, authorization. (Sahaj's doc.) | Synchronous read, async write |
| **Extractor** | Conversation → passport draft → review queue → publish | Fully asynchronous |

The Gateway is the adoption mechanism. The Context Bus is the product. The Extractor is
where quality is won or lost.

**Why a proxy and not MCP or hooks** — the one-sentence answer to "how is this different
from qm / deja-vu / agentmemory": MCP tools are **pull** (nothing in the protocol makes a
model read before acting), hooks are **push** but per-harness and non-portable. A gateway
is the only mechanism that is **both push and harness-agnostic**. Every competing project
sits on one horn of that tension; this sits on neither.

```
   dev's harness (Claude Code / Cursor / anything)
        │  POST /v1/messages   (ANTHROPIC_BASE_URL points here)
        ▼
  ┌──────────────────────── GATEWAY ────────────────────────┐
  │ 1 authenticate → principal set                          │
  │ 2 DLP scan outbound  → tokenise                         │
  │ 3 derive retrieval query from conversation              │
  │ 4 ── Context Bus search (authorized) ──►                │
  │ 5 assemble context, dedup vs already-injected           │
  │ 6 inject as role:"system" message at end of messages[]  │
  │ 7 forward upstream ──────────────────────────────────►  │
  │ 8 stream SSE back, restoring DLP tokens                 │
  │ 9 tee turn ──► extraction queue                         │
  └─────────────────────────────────────────────────────────┘
        │                                    │
        ▼                                    ▼
   dev sees normal response          EXTRACTOR (async)
                                     draft → dedup → contradiction
                                     → review queue → human approve
                                     → publish → embed → index
```

---

## 1. The one correctness bug in the current design

**Post-filter authorization cannot return the top-n authorized results.**

The current plan is: ANN search top-k → check permissions → drop unauthorized → return.
Then "if too many are removed, search top 100 or top 200."

There is no value of `k` that makes this correct. If a user is authorized to see 1% of
passports, the top-200 ANN neighbours may contain zero of them while the 5,000th-nearest
is a perfect match. The failure is silent: the user gets a plausible-looking short list
and no signal that the real answer was never a candidate. That is worse than an error —
it looks like "the bus has nothing on this."

### Fix: index-assisted pre-filter

Two pieces, both cheap:

**(a) Materialize the authorization set onto the searchable row.** Keep the
`passport_permissions` table as the source of truth for grants — that part of the design
is right. But denormalize the *resolved* set onto the row the search touches:

```sql
ALTER TABLE passport_embeddings
  ADD COLUMN visible_to bigint[] NOT NULL;      -- principal ids, any type
CREATE INDEX ON passport_embeddings USING gin (visible_to);
```

Then a request resolves the caller once into their transitive principal set
(`[org:1, dept:7, team:22, project:5, role:sre, user:314, group:oncall]`) and the search
becomes:

```sql
SELECT passport_id
FROM   passport_embeddings
WHERE  visible_to && $1::bigint[]              -- GIN, pre-filter
ORDER  BY embedding <=> $2
LIMIT  20;
```

**(b) Turn on iterative index scans** so HNSW keeps walking until it has actually filled
`LIMIT 20` with matching rows, instead of scanning `ef_search` candidates and handing
back whatever survived:

```sql
SET hnsw.iterative_scan = relaxed_order;
SET hnsw.max_scan_tuples = 20000;              -- bounded worst case
```

> **Checkable, verify before building on it:** iterative index scans landed in
> **pgvector 0.8.0**. Run `SELECT extversion FROM pg_extension WHERE extname='vector';`
> If you're below 0.8, either upgrade or accept post-filtering with an explicit
> "results may be incomplete" flag on the response — but don't ship silent incompleteness.

**Cost of the denormalization:** a grant change requires a fan-out update to
`visible_to`. That is the trade, and it's the right one — grants change rarely, reads are
hot, and the fan-out is a background job. Keep the permissions table authoritative so you
can always rebuild `visible_to` from scratch.

**[KEEP for demo]** — this is ~15 lines of SQL and it is the single most defensible
technical claim in the pitch. "Our retrieval is authorization-correct, not
authorization-filtered" is a sentence no competing team will be able to say.

### What's still missing from the authz model

- **user → principals resolution** is undefined. Group/dept/team membership is transitive
  (a user in team → dept → org). Needs a resolver with a short-TTL cache; it runs once
  per request and its output is the `$1` array above. **[CUT for demo:** flat seeded
  memberships, no transitivity.**]**
- **Deny rules and precedence.** Currently grants only. If ESDS needs "dept X can see
  this except contractors," you need deny + a stated precedence order. **[CUT]**
- **Write authorization.** Who may publish a passport claiming "we decided X"? An
  agent-written, wrong, authoritative-looking passport is worse than no passport. The
  human review queue is the control — keep it.

---

## 2. Read path

### 2.0 Who is this? — identity and enrolment

Every authorization claim in §1 rests on the gateway knowing *which human* a request
belongs to. This was undefined and it is foundational — get it wrong and the demo's two
personas are indistinguishable.

**Verified on Sahaj's machine, 2026-08-07 — both auth modes are live at once:**

| Mode | What the harness sends | Usable as identity? |
|---|---|---|
| API key (`ANTHROPIC_API_KEY` exported) | `x-api-key: sk-ant-…` | Yes — stable, forwardable, mappable |
| Claude.ai OAuth (`/login`, `~/.claude/.credentials.json`) | `Authorization: Bearer sk-ant-oat01…` + `anthropic-beta: oauth-2025-04-20` | **No** — rotates on refresh (`expiresAt` + `refreshToken`), and the gateway can't introspect it for a user |

Credential resolution is **first-match-wins**: `ANTHROPIC_API_KEY` → `ANTHROPIC_AUTH_TOKEN`
→ OAuth profile. So an exported API key silently shadows a `/login` session. Sahaj is on
API-key auth; any teammate who only ran `/login` is on subscription OAuth. **Assume both
will show up.**

**Decision: the gateway issues its own keys.** Enrolment is two env vars, which is config,
not a new tool — the adoption constraint holds:

```
ANTHROPIC_BASE_URL=https://passport.esds.internal
ANTHROPIC_API_KEY=dp_live_<per-developer>
```

The gateway maps `dp_*` → principal set, and holds **one** upstream Anthropic credential of
its own. This wins over both alternatives:

- vs. forwarding the dev's own credential: works identically for OAuth and API-key devs,
  no rotation problem, no shared-team-key ambiguity.
- vs. deriving identity from the incoming token: nothing to derive for OAuth, and setting
  `ANTHROPIC_API_KEY=dp_…` shadows an existing `/login` session automatically — nobody has
  to log out.

Reject requests carrying an upstream-shaped credential (`sk-ant-*`) with a clear error
telling the dev to enrol, rather than silently proxying them unattributed.

**Two consequences somebody has to sign off on, not implementation details:**

1. **Billing and rate limits move.** A subscription-auth developer proxied through the
   gateway now bills to the org's API key and draws on its rate limits instead of their
   personal `rateLimitTier`. For an enterprise that's arguably the point — central
   billing, central limits, central audit — but it is a policy change, and per-model rate
   limits are separate buckets (Opus 5 does not share the Opus 4.x pool), so size them.
2. **The gateway holds one high-value credential.** It becomes the blast radius. Vault it,
   rotate it, and never let it reach a log line.

**[CUT for demo:** no self-serve enrolment UI — a seeded `gateway_principals` table with
5 hard-coded `dp_*` keys. **KEEP:** the `sk-ant-*` rejection, so an unenrolled teammate
gets an error instead of a silently unattributed session.**]**

### 2.1 A proxy has no query

This is the structural cost of choosing the gateway. A tool-call surface receives the
agent's own search string; a proxy must *infer* what to retrieve. Options:

| Option | Latency | Quality |
|---|---|---|
| (a) Embed the last user turn verbatim | ~5ms local | Fails on anaphora ("what about the other approach?") |
| (b) Embed last user + last assistant turn | ~5ms | Noisier, handles anaphora better |
| (c) Cheap LLM call to synthesize a query | +200–400ms **on every request** | Best |
| (d) (a) by default, escalate to (c) when the turn is short/anaphoric | mixed | Best per rupee |

**Recommendation:** (b) for the demo, (d) for production. Do not put (c) unconditionally
in the request path — it doubles perceived latency on every single call and that is the
fastest way to get the gateway turned off.

Note the passport schema is *decision-shaped* (`subject` / `claim`). That means retrieval
should match on **subject**, not on the whole turn. Longer term, extracting the subject
and entities from the turn beats embedding the turn. **[CUT for demo]**

### 2.2 Injection point — the load-bearing decision

**Inject retrieved context as a `{"role": "system", "content": "..."}` message appended
to `messages[]`. Never by editing the top-level `system` field.**

Three reasons, all of them load-bearing:

1. **Prompt cache survives.** The API renders `tools` → `system` → `messages` and the
   cache is a prefix match — any byte change invalidates everything after it. Editing the
   top-level `system` prompt changes the prefix ahead of the *entire* conversation
   history, so every previously-cached turn is reprocessed uncached. A `role: "system"`
   message sits *after* the cached history and invalidates nothing. Getting this wrong
   makes the gateway roughly 10× more expensive per call and raises time-to-first-token
   on every request — the two things that get an interception layer removed.
2. **Operator authority, non-spoofable.** Content inside a user turn can be forged by
   anything that writes to user-visible input. A `role: "system"` message cannot.
3. **No beta header required.**

Constraints to code against: must follow a `user` message (or an `assistant` message
ending in server-tool use); must be either last in `messages` or followed by an
`assistant` turn; cannot be `messages[0]`; content is text-only.

**Model gating — this is a real branch.** Mid-conversation system messages are supported
on Claude Opus 5, Claude Opus 4.8, Claude Fable 5, Claude Mythos 5 — and **not** on
Claude Sonnet 5. An unsupported model returns
`400 role 'system' is not supported on this model`. Since the gateway does not control
which model the developer's harness picks, it needs:

```
try   inject as role:"system"
catch 400 "role 'system' is not supported"
      → fall back to a <system-reminder> text block in the user turn
        (same caching profile; spoofable, so mark it lower-trust)
      → cache the capability per model id
```

**[CUT for demo:** hard-code the Opus 5 path, no fallback. **KEEP the try/catch** — it's
6 lines and it stops the demo dying if a teammate's editor is on Sonnet.**]**

### 2.2a The 20-block lookback — the one way this still taxes the cache

Injecting after the cached prefix costs one uncached block on the turn it lands, and from
the next turn on it sits inside the prefix. That much is fine and self-correcting.

The risk is elsewhere: **a cache breakpoint walks back at most 20 content blocks** looking
for a prior entry. A real Claude Code agentic turn with parallel tool calls routinely
emits more than 20 blocks (`tool_use` / `tool_result` pairs add up fast), and the gateway
adds one more per turn. If the lookback overshoots, you don't lose the injected tail — you
lose the **entire prefix** and reprocess the whole conversation uncached. That is precisely
the tax the pitch claims not to impose, and it will show up on long agentic sessions rather
than short chat ones, so a two-turn smoke test won't catch it.

Mitigations, in order:

1. **Measure first** (see §9 Day 1) on a genuinely multi-tool-call turn, not a chat turn.
2. If the lookback misses, have the gateway place its own `cache_control` breakpoint on the
   injected block. Budget carefully: **4 breakpoints per request maximum**, and the
   harness upstream is already using some. Read how many are present before adding one, and
   never exceed the ceiling — an over-budget request is rejected, which is a hard failure
   in the request path.
3. Watch `cache_read_input_tokens` continuously; a drop to zero is the alarm.

### 2.3 The conversation carries its own injection state

The proxy is stateless; every request arrives with the full history. So: **do not build a
server-side session store to track what you've already injected.** Instead, stamp each
injected block with a machine-readable marker and read it back out of the incoming
history:

```
<data-passport injected="p_8a3f,p_91c2,p_004e" v="1">
  ... rendered passports ...
</data-passport>
```

On each request, scan `messages[]` for prior markers, union the passport ids, and inject
only what's new. Benefits: no session identity problem, no store to keep consistent, and
the conversation transcript is self-describing — you can reconstruct exactly what context
a given answer saw, from the transcript alone. (Same property that makes Claude Code's
append-only JSONL replayable.)

This also means injected context **accumulates by appending**, never by rewriting — which
is the only option that preserves the cache anyway. So the token budget in §2.4 is a
budget *per turn*, and you need a running total.

### 2.4 Context assembly

- **Token budget:** cap at ~1,500–2,000 tokens per turn and ~5% of the model's context in
  aggregate. Count with `messages.count_tokens`, not a character heuristic.
- **Order:** by score descending; the model weights early content more.
- **Citations:** emit `passport_id` per item so answers can reference them and so the
  feedback loop (§5) can attribute.
- **Staleness:** render `age`, and explicitly render `superseded_by` warnings. A stale
  passport presented as current is the failure mode that destroys trust in the product.
- **Framing:** wrap in a delimited block that says this is retrieved reference data, not
  instructions. See §6 on why.

### 2.5 Latency budget

Everything here is on the critical path of every LLM call. Injection must complete before
the first upstream token, so it *blocks*.

| Step | Budget |
|---|---|
| Auth + principal resolution (cached) | 5 ms |
| DLP scan (deterministic) | 10 ms |
| Query embedding (local model, in-process or sidecar) | 15 ms |
| Vector search + authz pre-filter | 30 ms |
| Assembly + injection | 10 ms |
| **Total** | **~70 ms, hard ceiling 150 ms** |

Serve the embedding model **locally** — in-process or as a sidecar, never over the public
internet. This is a real argument in favour of the self-hosted BGE/Nomic/E5 choice, quite
apart from model quality.

### 2.6 Fail-open, always

**Any** Context Bus error, timeout, or circuit-breaker trip → forward the request
unmodified and log. The gateway is in the path of every AI call at ESDS; a bus outage must
degrade retrieval, never break the developer's editor. This is ~10 lines and it is
non-negotiable. **[KEEP for demo]** — it's also what saves a live demo from a hiccup.

---

## 3. Storage schema

The three-table split (passports / embeddings / permissions) is right. The gaps below are
the ones that kill organizational-memory systems in month three.

### 3.1 `passports` — additions to the current draft

```
passport_id        uuid pk
org_id             bigint            -- tenancy key, present from day one
subject            text
subject_key        text              -- normalized subject, the clustering key
claim              text
alternatives_rejected  jsonb
reason             text
author_principal   bigint
created_at         timestamptz

-- MISSING FROM THE CURRENT DESIGN, all cheap, none backfillable:
status             text              -- draft | active | superseded | retracted
supersedes         uuid null         -- fk passports
superseded_by      uuid null         -- fk passports
valid_from         timestamptz
valid_until        timestamptz null
confidence         real
source_ref         jsonb             -- session id, turn uuid, upstream request_id
contradiction_flags jsonb            -- [{other_passport_id, kind, adjudication}]
metadata           jsonb
```

**Why `subject_key` earns its place:** it makes both dedup and contradiction detection
O(cluster) instead of O(corpus). You only ever compare a new passport against *active*
passports sharing its `subject_key` — usually 0–5 rows. That's what makes contradiction
detection cheap enough to demo, which per the brief is the differentiating claim.

**Why supersession is not optional:** without it, two years of passports about the same
decision all read as equally current, and the bus starts actively misleading people. This
is the single most common way these systems fail, and it cannot be retrofitted onto
un-versioned rows.

**Chunking:** deliberately none. Passports are small and semantically atomic. That is a
genuine strength of the passport framing over document RAG — say so in the pitch.

### 3.2 `passport_embeddings` — needs a lifecycle, not just a column

```
passport_id        uuid
embedding_model    text
embedding_version  int
embedding          vector(768)
embedding_text     text        -- the EXACT text that was embedded
status             text        -- pending | active | superseded
visible_to         bigint[]    -- denormalized authz, see §1
created_at         timestamptz
primary key (passport_id, embedding_model, embedding_version)
```

Two additions worth arguing for:

- **`embedding_text`** — store the rendered string that was actually fed to the encoder
  (a fixed template over subject + claim + reason). Without it, re-embedding is not
  reproducible and you can't A/B two templates. Which text you embed is a real retrieval
  quality decision, and right now it's undefined.
- **`status` + a config pointer.** The read path selects
  `WHERE embedding_model = $active_model AND embedding_version = $active_version`, read
  from one config row. A backfill worker writes `pending` rows for a new model; when the
  count matches, flip the pointer in one transaction. That is what makes "migrate to a
  different embedding model" an actual operation rather than an aspiration. **[CUT for
  demo:** one hard-coded model.**]**

### 3.3 `passport_permissions` — unchanged, and correct

The `(passport_id, principal_type, principal_id, permission)` shape is the right call —
it keeps new principal types from becoming schema migrations. Keep it authoritative;
`visible_to` is a derived cache of it.

### 3.4 Tenancy — the mechanism behind "one index per org"

"One HNSW index per organization" is achievable, but the mechanism matters. Partial
indexes per tenant do not scale past a few dozen. The right answer in Postgres is
**declarative partitioning of `passport_embeddings` by `org_id`** — indexes are per
partition automatically, so you get per-org HNSW graphs for free, and `WHERE org_id = $1`
prunes to one partition.

Partition **from day one if multi-tenancy is at all likely**: it's nearly free now and a
painful migration later. **[CUT for demo:** single table, `org_id` column present but
unpartitioned.**]**

Do **not** partition by department — the doc is right that departments share knowledge,
and department is a filter, not a boundary.

---

## 4. Write path

The current diagram (`conversation → extraction → embedding → store`) hides the hardest
part of the system.

### 4.1 Stages

```
tee from gateway
  └─► turn queue        (messages + response + principal + request_id + timestamps)
        └─► EXTRACTOR   Opus 5 + structured outputs → 0..n passport drafts
              └─► DEDUP        vs active passports sharing subject_key
                    ├─ near-identical  → drop, bump confidence on existing
                    ├─ refinement      → draft with supersedes = existing
                    └─ novel           → new draft
                    └─► CONTRADICTION  one LLM adjudication vs same-subject_key actives
                          └─► REVIEW QUEUE  human approve / edit / reject
                                └─► PUBLISH  status=active, supersede predecessor
                                      └─► EMBED + index
```

### 4.2 What the current design leaves undefined

- **What triggers extraction.** Every turn is far too expensive and produces noise.
  Options: session end, N-turn window, or a cheap classifier gating "did a decision get
  made here?". **[Demo: a manual trigger.** Extraction on a button is *more* demoable than
  extraction on a timer, because it's deterministic on stage.**]**
- **Idempotency.** A multi-turn conversation streams past the tee many times. Key
  extraction on `(session_id, turn_range)` and make the writer idempotent, or you'll
  extract the same decision five times.
- **Use structured outputs, not prose parsing.** `output_config: {format: {type:
  "json_schema", schema: PASSPORT_SCHEMA}}` — or `client.messages.parse()` with a Pydantic
  model. The schema *is* the passport shape, which means the extractor cannot emit a
  passport that doesn't fit the table. Note the JSON Schema restrictions: every object
  needs `additionalProperties: false`, and numeric/length constraints aren't supported
  (the Python/TS SDKs strip them and validate client-side).
- **Cache the extractor's own prefix.** The extraction system prompt + schema are
  byte-identical across every extraction. Put a `cache_control` breakpoint at the end of
  the system block and every extraction after the first reads at ~0.1× input cost.
  Verify with `usage.cache_read_input_tokens`; if it's 0 across runs, something dynamic
  (a timestamp, an unsorted `json.dumps`) is in the prefix.
- **Do not let secrets into passports.** Run DLP on extractor *output* as well as gateway
  input.

### 4.3 Contradiction detection — the differentiator, made cheap

Because `subject_key` clusters, detection is:

1. New draft arrives with `subject_key = K`.
2. Fetch active passports where `subject_key = K` (typically 0–5 rows).
3. One Opus 5 call with a structured verdict:
   `{relation: same|refines|contradicts|unrelated, confidence, explanation}`.
4. `contradicts` → write `contradiction_flags` on both rows, surface in the review queue
   and in retrieval output.

That is one LLM call per publish, on the async path, at negligible volume. **[KEEP]** —
per the brief this is the claim no competing team will demo, and it falls out of the
schema for almost nothing.

---

## 5. DLP

Deterministic first. **Do not put an LLM in the request path as the credential gate** —
it's probabilistic on a security control and it adds 300ms to every call.

- **Detectors:** regex + Shannon entropy for a fixed high-value set (AWS keys, private
  key blocks, JWTs, connection strings, bearer tokens) plus NER for PII. For ESDS,
  include India-specific formats — PAN, Aadhaar, IFSC.
- **Tokenise-and-restore**, not `[REDACTED]`: replace the span with a stable opaque token,
  hold the mapping in an ephemeral per-request/session vault (Redis, short TTL, encrypted
  at rest), restore on the way back. The model reasons over a placeholder; the developer
  sees their real value. This is the right call and it's a much better demo than redaction.
- **Streaming gotcha, budget time for it:** a token can be split across SSE chunks, so the
  restorer needs a boundary-aware buffer (hold back `max_token_len - 1` bytes) rather than
  a per-chunk `str.replace`. This is the one part of the DLP story that will eat an
  afternoon.
- **Scope:** scan outbound requests, extractor output, *and* retrieved context before
  injection.
- **Policy toggle, not per-event approval** — matches the decided read-path posture.

---

## 6. Prompt injection — state the boundary precisely

The `role: "system"` channel is injection-safe in one specific sense: **user input cannot
forge it.** It says nothing about the trustworthiness of content *you chose* to inject.
Passports are written by other people and other agents, and you are putting them into
someone else's context at operator authority. That raises the stakes on content trust, it
doesn't lower them.

Controls, in order of strength:

1. **Human-approved publish** (already the design) — the real control. Keep it.
2. **Delimited, framed block** — retrieved passports go inside a marked container with an
   explicit "reference data, not instructions" framing.
3. **Provenance on every item** — author principal rendered inline, so a suspicious claim
   is attributable.
4. **Never inject an unreviewed draft.** Ever.

---

## 7. Observability — the rows nobody can backfill

Three tables to write from day one even with no UI on top. They are worthless if started
later.

| Table | Contents | Why now |
|---|---|---|
| `access_log` | who read which passports, when, via which request | Compliance requirement for an access-controlled store at ESDS |
| `passport_usage` | injected → cited/used, per turn | The only signal that can ever improve ranking beyond raw cosine |
| `dlp_events` | detector hits, class, action | The "PII checkpoint" claim needs evidence |

**[KEEP for demo:** write the rows, skip the UI. `passport_usage` in particular is the
seed of the whole feedback loop and cannot be reconstructed.**]**

---

## 8. Model choices

| Job | Where | Recommendation |
|---|---|---|
| Passport extraction | async | **`claude-opus-5`**, adaptive thinking, structured outputs. It's a judgment call (decision vs chatter), latency is irrelevant, and a human reviews it. |
| Contradiction adjudication | async, low volume | `claude-opus-5`, structured verdict |
| Query synthesis (if adopted) | **request path** | `claude-haiku-4-5` — latency-critical and simple. **Your call**, and it's the only place where the cheaper tier is clearly right. |
| DLP detection | request path | **No LLM.** Regex + entropy + NER. LLM only as an *offline* auditor of what the deterministic pass missed. |
| Embeddings | request path | Self-hosted BGE / Nomic / E5, local. Anthropic has no embedding endpoint, so this stays your own infra — which is the right answer for the latency budget anyway. |

Prompt caching applies to the extractor and the adjudicator: stable system prompt +
schema, one breakpoint at the end of the system block. Cache reads are ~0.1× input;
writes are 1.25× at the 5-minute TTL, so break-even is two requests. Minimum cacheable
prefix is 512 tokens on Opus 5 (1024 on Opus 4.8 / Sonnet 5) — a short extraction prompt
may fall under it and silently not cache, so check `cache_creation_input_tokens` is
non-zero.

If data residency matters for ESDS, `inference_geo` is a top-level request parameter
(supported Opus 4.6 / Sonnet 4.6 and later); `response.usage.inference_geo` reports where
inference actually ran. Worth one line in the pitch, and it's a differentiator for an
Indian enterprise audience.

---

## 9. Three-day plan

Demo spine: **intercept → DLP gate → async extract to a structured Decision record →
authorization-correct semantic retrieval → inject without breaking the cache → catch a
contradiction between two teams.**

### Day 1 — spine, and two measurements that can still change the build
- Gateway skeleton: reverse proxy over `/v1/messages`, SSE passthrough, fail-open
- `gateway_principals` with 5 seeded `dp_*` keys; reject `sk-ant-*` (§2.0)
- Postgres + pgvector, three tables, `visible_to` + GIN, `hnsw.iterative_scan`
- **Measurement A — the cache baseline.** Run the *same* realistic agentic conversation
  (several turns, parallel tool calls, >20 blocks in a turn) twice: once with
  `ANTHROPIC_BASE_URL` unset, once set. Compare `cache_read_input_tokens` per turn. This is
  **gateway vs no gateway** — not gateway vs a naive strawman. If the delta is bad, you have
  two days to fix it with breakpoint management (§2.2a); on day 3 you have none.
- **Seed data — make the authz claim falsifiable.** With 30 passports and 2 departments,
  post-filter and pre-filter return *identical* results and the §1 claim is an unverifiable
  slide assertion. Deliberately construct one query where the authorized answer sits well
  outside the top-50 by cosine: ~200 semantically-near passports the user *cannot* see,
  and the one they *can* see further out. Then show both code paths side by side —
  post-filter returns nothing, pre-filter returns the answer. This is a seed-data task, not
  a code task, and it is the difference between a claim and a demo.

### Day 2 — the two claims
- DLP: detectors + tokenise/restore + the streaming boundary buffer
- Extractor: Opus 5 + structured outputs, manual trigger, review queue (plainest possible
  web list)
- Contradiction: `subject_key` cluster + one adjudication call

### Day 3 — the demo
- Two personas, two harnesses (Claude Code + one other) hitting the same bus
- Scripted narrative: dev A makes a decision → passport published → dev B in a *different
  editor* gets it injected unasked → dev C tries to contradict it → flagged
- Side-by-side authz demo off the day-1 seed set: post-filter finds nothing, pre-filter
  finds the answer
- One slide with Measurement A from day 1: `cache_read_input_tokens` **with the gateway vs
  without it**, same conversation. That number is the whole "we don't tax your workflow"
  argument, and it only lands because the comparison is against the dev's status quo rather
  than a strawman we built ourselves.
- Slides for: multi-tenant partitioning, embedding migration, transitive groups, deny
  rules, ranking feedback

### Explicitly cut, and say so out loud
Transitive group expansion · deny rules and precedence · embedding version migration ·
multi-tenant partitioning · reranking · decay/TTL · per-event read approval ·
audit and usage UIs (rows written, no screens) · Sonnet-5 injection fallback path

---

## 10. Open questions

1. **Extraction trigger** in production — session end, N-turn window, or gated classifier?
   Affects cost more than anything else in the system.
2. **Conversation identity** across harnesses. The self-describing marker (§2.3) sidesteps
   this for injection state, but `passport_usage` attribution wants a stable session key.
   Does the upstream request carry anything usable, or does the gateway hash the prefix?
3. **`subject_key` normalization.** LLM-assigned, or a controlled vocabulary? LLM is
   flexible and drifts; controlled is stable and needs curation. This decides whether
   clustering still works at 10k passports.
4. **Review queue throughput.** Human approval is the correctness control and the
   bottleneck. At what passport-per-day rate does it stop being viable, and what's the
   escape hatch — auto-publish above a confidence threshold, per-domain delegated
   reviewers, or something else?
