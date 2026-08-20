# Search, Capture, and UI — what to change

Answers five questions: why search returns bad results, what to capture from a session,
how to render it, what the Context Bus UI should show, and how to trace a request.

Ordered by **what the audience sees**, not by engineering interest.

---

# Part 1 — Why search is bad

Three separate causes. They are not equally important.

## Cause 1 (dominant): your knowledge base is 85% test junk

202 records in `store/bronze/`. Here is what's actually in them:

| Count | Title |
|---:|---|
| 77 | `"t"` |
| 40 | `"Race probe"` |
| 10 | `"Bus test entry"` |
| ~20 | `"Redis eviction policy decision <random hex>"` (and `… ORG` duplicates) |
| 25 | `"Private platform record about auth tokens"` and friends — visibility fixtures |
| 5 | *(no title / no knowledge block at all)* |
| **~25** | **actual content** — Kafka tuning, token refresh race, a few real ones |

And the real ones aren't much better. A genuine `claude-code` capture, verbatim:

```json
{
  "title": "Session mode: Ship",
  "summary": "Sahaj activated Ship mode, prioritizing execution with minimal
              deliberation. Only dangerous decisions get flagged in one sentence.",
  "outcome": "decision_made"
}
```

That is **session chatter stored as organizational knowledge.** No ranking algorithm
rescues a corpus like this. When you type `ESDS_SEARCH authentication` and get back
`"t"` and `"Session mode: Ship"`, search is working correctly — it is faithfully
retrieving the nearest neighbours in a landfill.

**Fix — zero code, largest visible effect:**

```bash
# wipe
docker exec -it <postgres-container> psql -U <user> -d <db> \
  -c "TRUNCATE knowledge_entries, knowledge_embeddings, context_bus_events, redaction_audit_log;"
rm -rf store/bronze/*

# reseed with 5-6 real records, through the real path
#   ESDS_SUBMIT  →  ESDS_APPROVE <id> --visibility org
```

Seed records that answer questions a judge might ask. Suggested set:

1. Auth — the CORSMiddleware fix (matches Scenario 2 in the cheatsheet)
2. Caching — Redis over direct Postgres for the analytics dashboard
3. Frontend — Vite + React over Next.js, no SSR needed
4. Infra — Kafka partition sizing
5. A record from a *different* team at `--visibility team`, to demonstrate that
   retrieval genuinely refuses it

Number 5 is the best demo you have and you don't currently run it. **Show a search
that correctly returns nothing** because the caller isn't authorised. That is the
Glean §2.2 claim, demonstrated live rather than asserted.

## Cause 2: you embed one text, search a second, display a third

| Stage | Text used | Where |
|---|---|---|
| Vector embedding | `content` — the raw 2-4 sentence session blob | `store/backend/app/main.py:210` |
| Keyword arm | `title`, `summary` | `store/backend/app/serving.py:86` |
| What the user sees | `title`, `summary` | `gateway/policies/read.py:126-133` |

Three different texts. The vector index is built over prose nobody reads, while the
thing that gets matched literally and the thing that gets displayed are something else.

Worse: a **query is a question** ("how do we handle auth?") and `content` is
**transcript-shaped**. Those embed into different regions. `title + summary` is
answer-shaped and much closer to what a question retrieves.

**Fix — one line, `main.py:210`:**

```python
# before
embedding = list(get_embedding_model().embed([content]))[0].tolist()

# after
_k = body.get("knowledge") or {}
_embed_text = "\n".join(filter(None, [
    _k.get("title"),
    _k.get("summary"),
    " ".join(_k.get("key_points") or []),
    content,
]))
embedding = list(get_embedding_model().embed([_embed_text]))[0].tolist()
```

**There is no re-embed script in the repo.** Old rows keep `content`-derived vectors,
and mixing them makes distances incomparable. So this change *requires* a wipe — which
is Cause 1's fix. **Do them together and the second one is free.**

## Cause 3: ranking is pure cosine distance

`serving.py:104` — `ORDER BY distance ASC`. Nothing else.

Your own Glean teardown, §2.4, records the point: Glean ranks using the graph and
activity signals, **not embedding similarity alone.** You already store `outcome`,
`status`, `created_at`, `department`, `team`, `author_user_id` and use none of them
for ranking.

Also note: the keyword arm only contributes *candidates*. The final sort is cosine, so
an exact title match can be cut by the `LIMIT` — while `gateway/policies/read.py:101`
comments that a keyword hit "is kept: it matched." The store does not honour that.

**Roadmap, not today.** If you did have an hour, the cheapest version:

```sql
ORDER BY distance
       - CASE outcome WHEN 'decision_made' THEN 0.05
                      WHEN 'issue_resolved' THEN 0.05
                      WHEN 'question_open' THEN -0.05 ELSE 0 END
       - GREATEST(0, 0.05 - EXTRACT(EPOCH FROM (now() - created_at))/86400 * 0.001)
  ASC
```

Decisions and resolutions outrank open questions; recent outranks stale. Say this is
next; don't build it before the demo.

---

# Part 2 — What to capture from a session

Current shape (`gateway/policies/write.py:59-79`):

```
content, knowledge.{title, summary, outcome, key_points, next_steps}, status
```

Three additions, in order of value. All are edits to one string — the extraction prompt.

## 2a. Capture the PROBLEM, not just the answer — highest leverage

People search with **questions**. You store only **answers**. Add:

```
"problem": "<the question or failure that started this — in the words someone
             hitting it again would use>"
```

Then include `problem` in the embedded text (Cause 2's fix). Query-document symmetry
is the single biggest cheap win in retrieval quality: a question in the corpus matches
a question in the query box.

## 2b. Capture concrete nouns

`knowledge_entries.entities` is a real JSONB column and **nothing ever writes it**.

```
"entities": ["<files, repos, services, libraries, endpoints actually touched>"]
```

People search for `CORSMiddleware`, `payments-api`, `Redis` — proper nouns. Prose
summaries bury them; the keyword arm can't find what isn't in `title`/`summary`.

## 2c. A refusal clause — this is what stops the landfill refilling

Append to the extraction instruction:

```
If this session did not actually decide anything, resolve anything, or learn
anything a colleague could act on, output NO json block at all and tell the user
there was nothing worth saving. Session setup, mode changes, tool configuration,
and routine questions are NOT knowledge. Do not save them.
```

That one paragraph is why `"Session mode: Ship"` would never have been stored.

Note: `flows.handle_write_response` already treats "no draft block" as
`reason="no_draft_block"` and saves nothing, so the refusal path already works — the
model was simply never told it was allowed to refuse.

---

# Part 3 — How results are shown

Current render (`read.py:111-139`) produces:

```
[passport a3f9c1d2] Push retry policy
  Exponential backoff, capped at 30s...
  — Engineering/platform, 2026-08-09, outcome=decision_made, status=completed
```

Three things missing, all cheap:

## 3a. Nobody's name appears

Your README's pitch is *"Priya's session from last month is retrieved."* The renderer
shows `department/team` and never says **who**. `author_user_id` comes back in the
search result and is dropped on the floor at `read.py:92-96`.

`— Priya (Engineering/platform), 3 weeks ago` is a different product from
`— Engineering/platform, 2026-08-09`. It makes the record feel like a *person's work*,
which is the entire emotional pitch.

## 3b. Relative time, not ISO dates

`3 weeks ago` tells you whether to trust it. `2026-08-09` makes you do arithmetic.
(Bonus: your PII detector currently tokenizes every `YYYY-MM-DD` it sees — see
`DEMO-RISKS.md` — so relative dates dodge that too.)

## 3c. No indication of *why* it matched

Nothing shows relevance. Add the match strength — you already compute `distance`:

```
[passport a3f9c1d2] Push retry policy                    ●●●○ strong match
```

Showing the system's confidence is what separates a search product from a database
dump. Glean does it; every good search UI does it.

**Suggested render:**

```
[passport a3f9c1d2] Push retry policy                          ●●●○ strong match
  Problem: retries stampeded the gateway after a 30s outage
  Decision: exponential backoff capped at 30s, jittered
  — Priya (Engineering/platform), 3 weeks ago · touches: payments-api, retry.go
```

Problem-then-decision is readable at a glance and gives the model a cleaner
attribution structure than one undifferentiated summary blob.

---

# Part 4 — The Context Bus UI

## The finding: you built three brain surfaces and wired zero of them

| Endpoint | What it gives you | Used by the dashboard? |
|---|---|---|
| `GET /v1/search` | permission-filtered semantic search | yes — with `q='.*'`, which matches nothing |
| `GET /v1/bus/subscribe` | **live SSE feed of passports as they land** | **no** |
| `GET /v1/agent-activity` | **who is working on what, right now** | **no** |
| `GET /v1/knowledge/{id}` | full record detail | **no** — cards are dead ends |

The current tab is a card grid plus a **Filter button, a Sort button, and a New Context
button that all do nothing**, under a search box in the top bar that also does nothing.
That reads as a mockup, and a judge will click one.

## What to build, ranked by demo value per minute

**1. Make the search box work.** ⭐ Highest value by a wide margin.

You are pitching an enterprise **search** product whose search box is decorative.
Wire the existing top-bar input to `GET /v1/search?q=<input>` and render the same
cards. Delete Filter / Sort / New Context — three dead buttons removed is a bigger
credibility win than any new view.

Show per result: title, problem, decision, author, relative time, visibility badge,
and the **match strength**. Facet chips for department/team/outcome if there's time.

**2. Live feed from the bus.** ⭐ This is what makes it look like a *brain*.

`GET /v1/bus/subscribe` is a real SSE endpoint that emits every passport as it commits.
A panel where a record **animates in the moment you type `ESDS_APPROVE`** is the single
most impressive five seconds available to you, and the backend already exists.

⚠️ Constraint: the endpoint holds a pool connection for the life of the stream and
asyncpg's default `max_size` is 10. One dashboard tab is fine. **Don't leave ten tabs
open** — you'll stall the whole bus mid-demo.

**3. "Who's working on what" from `/v1/agent-activity`.**

Latest session per agent, permission-filtered. This is the org-awareness view — the
answer to "what is my team doing right now." It's a plain SQL endpoint, already built
and tested, currently invisible.

**4. Make cards clickable → `/v1/knowledge/{id}`.**

Right now a card shows a truncated summary and nothing else. The full record has
`key_points`, `next_steps`, `open_questions`, `entities`, `domain_data`, the audit
trail. That's the depth that makes it look like a real knowledge base rather than a
list of headlines.

## What the "brain" UI should show — the frame

Three questions a brain answers, one per view:

| View | Question | Endpoint |
|---|---|---|
| **Search** | "What do we know about X?" | `/v1/search` |
| **Live feed** | "What is the org learning right now?" | `/v1/bus/subscribe` |
| **Activity** | "Who is working on what?" | `/v1/agent-activity` |

You have all three endpoints. You are showing none of them properly.

---

# Part 5 — How to trace a request

Everything the gateway does is logged to **`/tmp/dp_debug.log`** with a `[FLOW]` prefix
(`gateway/flows.py:81-85`) and echoed to the gateway's stdout.

**Set up one terminal for tracing:**

```bash
tail -f /tmp/dp_debug.log
```

And for the exact bytes leaving for Anthropic, start the gateway with:

```bash
DP_DEBUG_LOG_OUTBOUND=1 uvicorn gateway.app:app --port 8080
```

⚠️ That writes the **post-redaction** payload to `/tmp/dp_outbound_debug.json`. It is
the proof that the secret is gone — but any other sensitive content in the request lands
there in plaintext. Testing only.

---

## Tracing a READ (`ESDS_SEARCH`)

**Step 1 — type it.**
```
How do we handle auth? ESDS_SEARCH authentication
```

**Step 2 — did the marker get recognised?** Watch `/tmp/dp_debug.log`:
```
[FLOW] ESDS_SEARCH injected 3 record(s) for …
```
Nothing here → the marker wasn't in the last genuine human turn, or identity didn't
resolve. Check `store/config/account_map.json` has your `account_uuid`.

**Step 3 — what did the bus actually return?** Ask it yourself, same query, same token:
```bash
curl -s -H "Authorization: Bearer token-603cd1550b3d9dad" \
  "http://localhost:8000/v1/search?q=authentication&limit=5" | python3 -m json.tool
```
Compare the count to what the log said. Fewer in the log means the relevance floor
dropped some — `DP_SEARCH_MAX_DISTANCE`, default `1.0` (`read.py:84`).

**Step 4 — what did Anthropic actually receive?**
```bash
python3 -m json.tool < /tmp/dp_outbound_debug.json | less
```
Look for the injected block: either a literal `"role": "system"` message, or a
`<system-reminder>` folded into the last user turn. Which one depends on the model —
Sonnet 5 gets the fallback (`anthropic_adapter.py:22-27`).

**Step 5 — see it in the UI.** X-Ray Monitor, left pane vs right pane. Left is what you
typed; right is what left the machine.

**Where a READ usually goes wrong:**

| Symptom | Look at |
|---|---|
| Marker never fires | `markers.py` — must be line-start in the last human turn |
| `identity` unresolved | `account_map.json`, and `flows.py:119` fails closed |
| 0 hits from a live bus | the corpus (Part 1), or the relevance floor |
| Hits in the log, nothing in the answer | model gating — check `/tmp/dp_outbound_debug.json` for placement |

---

## Tracing a WRITE (`ESDS_SUBMIT` → `ESDS_APPROVE`)

Remember: **two turns, and the bus is only touched in the second one.**

### Turn 1 — `ESDS_SUBMIT`

**Step 1 — type it.**
```
We fixed the auth issue by adding CORSMiddleware to the FastAPI app. ESDS_SUBMIT
```

**Step 2 — the log should say:**
```
[FLOW] draft a3f9c1d2 captured and PENDING approval (redacted 0 value(s))
       — nothing written to the bus.
```

Failure modes visible right here:
- `no_draft_block` → the model didn't emit fenced JSON
- `invalid:<field>:<reason>` → schema validation failed (`write.py:155-193`)
- nothing at all → the marker wasn't recognised

**Step 3 — look at the parked draft on disk.**
```bash
ls -la /tmp/dp_pending/
cat /tmp/dp_pending/a3f9c1d2.json | python3 -m json.tool
```
Check `status: "pending_approval"`, and that `sensitivity_flags` matches what you'd
expect. **Confirm nothing is in the bus yet:**
```bash
curl -s -H "Authorization: Bearer token-603cd1550b3d9dad" \
  "http://localhost:8000/v1/search?q=CORSMiddleware" | python3 -m json.tool
# → empty. This is the approval gate, demonstrated.
```

That curl is worth running **on stage.** It's the proof of your central claim.

**Step 4 — see it in the UI.** Approval Inbox tab (`GET :8080/v1/dashboard/pending`).

### Turn 2 — `ESDS_APPROVE`

**Step 5 — type it, with the id and org visibility:**
```
ESDS_APPROVE a3f9c1d2 --visibility org
```

**Step 6 — the log shows the ingest.** Look for the record id, or:
- `queued_bus_unavailable` → bus is down; the payload is frozen and will retry on your
  next request (`flows.py:394`). **This is a feature — say so if it happens.**
- a `SURFACE`d 422 → schema rejection at the bus

**Step 7 — the raw payload landed in Bronze**, written before validation:
```bash
ls -lt store/bronze/Research_and_Development/claude-code/$(date +%Y-%m-%d)/ | head
```

**Step 8 — the record is now real:**
```bash
curl -s -H "Authorization: Bearer token-603cd1550b3d9dad" \
  "http://localhost:8000/v1/knowledge/<record_id>" | python3 -m json.tool
```

**Step 9 — and now retrievable.** Start a fresh Claude Code session and:
```
How do we handle auth? ESDS_SEARCH authentication
```
It comes back. That round trip — new session, no shared context, knowledge persists —
is the whole product in ten seconds.

**Step 10 — the audit trail:**
```sql
SELECT record_id, asserted_by_user_id, asserted_by_department, sensitivity_flags, outcome
FROM redaction_audit_log ORDER BY created_at DESC LIMIT 5;
```
Shows *who asserted* the PII flags. Enterprise buyers ask about audit; you have one.

---

# The build list, in order

| # | Change | Cost | Visible? |
|---|---|---|---|
| 1 | Wipe the corpus, reseed 5-6 real records at `--visibility org` | 10 min, no code | **enormous** |
| 2 | Make the top-bar search box actually search | ~30 min | **enormous** |
| 3 | Refusal clause in the extraction prompt | 5 min, one string | medium |
| 4 | Embed `title+summary+key_points+content` (`main.py:210`) — free, you're wiping anyway | 5 min | medium |
| 5 | Show author name + relative time + match strength in `render_documents` | 15 min | medium |
| 6 | Add `problem` and `entities` to the capture schema | 10 min | medium |
| 7 | Live SSE feed panel | ~45 min | **high, if time** |
| 8 | Clickable cards → `/v1/knowledge/{id}` | ~20 min | medium |
| 9 | Recency/outcome ranking | roadmap | — |

**If you only do two: 1 and 2.**

## One line to say on stage

Your Glean teardown (§2.2) records that query-time ACL enforcement is the axis —
snapshotting permissions at index time or filtering after generation are both strictly
weaker. **You enforce at query time, on every retrieval, against the caller's identity**
(`serving.py:35-40`). That is parity with Glean on the hard part, and demoing a search
that correctly returns *nothing* proves it in five seconds.
