# QA Test Guide — Data Passport Gateway

**Component under test:** `gateway/` (the interception proxy)
**Audience:** a tester with no prior knowledge of this project
**Prerequisite reading:** none — this document is self-contained
**Related:** `docs/GATEWAY-OVERVIEW.md` (what the component is), `TESTING.md`
(the developer's quick reference), `TEST-PLAN.md` (a *different* document — the
design-validation ladder, not a QA plan)

---

## 0. What you are testing, in one paragraph

The gateway is a reverse proxy that a developer's AI coding tool talks to instead
of talking to the model API directly. It must be **invisible** (the developer must
not notice it), it must **redact secrets and personal data** on the way out and put
them back on the way in, it must **retrieve organisational knowledge** only when
authorised and only after scanning it, it must **never let the AI write to
organisational memory without a human approving it**, and it must **never break a
session** even when its own logic fails.

**Two properties outrank everything else.**

1. **Invisibility (section T).** A gateway that is correct but adds visible latency,
   or makes output arrive in one burst, is a failed product — developers switch it
   off.
2. **No approval, no write (case W4).** If a draft can reach the Context Bus without
   a human typing `ESDS_APPROVE`, the product's central claim is false. Treat a W4
   failure as stop-ship regardless of what else passes.

---

## 1. Safety rules — read before running anything

| Rule | Why |
|---|---|
| **Never `export ANTHROPIC_BASE_URL`.** Always prefix it onto a single command. | Exporting redirects *all* Claude Code traffic in that shell, including unrelated work, for as long as it is set. |
| Use the **stub upstream** for everything except section L. | Tests are then free, deterministic, offline, and cannot leak anything to a third party. |
| Treat `fixtures/*.json` as **secret-bearing**. Never paste them into a ticket. | They are captured real request bodies and may contain real credentials. Gitignored for that reason. |
| Only enable `DP_DEBUG_LOG_OUTBOUND=1` for tests that require it; clear `/tmp/dp_outbound_debug_*` afterwards. | It writes request payloads to `/tmp` in plaintext. |
| Never put a **real** credential in a test string. Use the fake `sk-test-…` shape and obviously-fake PII. | A real value in a test is a real leak the moment someone shares a log. |
| Do not run `store/backend/tests/test_ingest.py` against a database holding demo data. | Its cleanup deletes **every** audit row with a NULL session id, not just its own. |

---

## 2. Setup

### 2.1 Three tracks

| Track | Needs | Cost | Sections |
|---|---|---|---|
| **A — gateway alone** | stub upstream | free, offline, no Docker | S, T, C, O, X |
| **B — with the bus** | stub upstream + Context Bus (Docker) | free, offline | M, R, W |
| **C — live** | real Anthropic API via Claude Code | uses the developer's plan | L |

Run A green before B, and B green before C.

### 2.2 Fast path — run the automated suite first

Before any manual testing, confirm the build is sane. This needs nothing running:

```bash
cd ~/Projects/hackathon_agent_layer
.venv/bin/python -m pytest gateway/tests -q          # expect: 141 passed
```

If that is red, stop and report it — manual testing on a red build wastes your time.

### 2.3 The stub upstream

It already exists at `scripts/stub_upstream.py` — **do not write your own**. It has
modes you will need:

| `DP_STUB_MODE` | Reply |
|---|---|
| `echo` (default) | `ECHO>> [user] <text>` |
| `verbatim` | the last human text block, unprefixed — needed whenever a marker must land at line start |
| `draft` | a canned fenced-JSON knowledge draft, for the write path |
| `fixed` | whatever `DP_STUB_REPLY` contains |

Other knobs: `DP_STUB_STATUS=500` forces an error response; `DP_STUB_CHUNK`
(default 4 bytes) and `DP_STUB_DELAY` (default 0.3 s) control SSE chunking. The
defaults are deliberately hostile — tiny chunks split redaction tokens across
boundaries, and the delay makes whole-response buffering obvious.

### 2.4 Start the servers

**Terminal 1 — stub upstream:**
```bash
cd ~/Projects/hackathon_agent_layer
.venv/bin/python -m uvicorn scripts.stub_upstream:app --port 9090
```

**Terminal 2 — gateway:**
```bash
cd ~/Projects/hackathon_agent_layer
DP_UPSTREAM_BASE_URL=http://127.0.0.1:9090 \
  .venv/bin/python -m uvicorn gateway.app:app --port 8080
```

Environment changes require **restarting terminal 2**. There is no hot reload.
Whenever a case lists a `Config`, restart with exactly that config.

**Terminal 3 (Track B only) — the Context Bus:**
```bash
cd ~/Projects/hackathon_agent_layer/store
docker compose up -d
cd backend && set -a && source ../.env && set +a
.venv/bin/python db/migrate.py
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Do **not** add `--reload`; it can serve stale code silently.
Confirm: `curl -s localhost:8000/openapi.json | head -c 40`

### 2.5 Test request bodies

```bash
mkdir -p /tmp/dp_t && cd /tmp/dp_t

# a plain streaming request, using the same metadata SHAPE Claude Code sends
cat > basic.json <<'EOF'
{"model":"claude-sonnet-5","max_tokens":64,"stream":true,
 "metadata":{"user_id":"{\"device_id\":\"dev1\",\"account_uuid\":\"aaaaaaaa-0000-4000-8000-000000000001\",\"session_id\":\"qa-session-1\"}"},
 "messages":[{"role":"user","content":[{"type":"text","text":"hello there"}]}]}
EOF

# a secret inside a tool_result — where credentials actually leak from
cat > toolresult.json <<'EOF'
{"model":"claude-sonnet-5","max_tokens":64,"stream":true,
 "metadata":{"user_id":"{\"device_id\":\"dev1\",\"account_uuid\":\"aaaaaaaa-0000-4000-8000-000000000001\",\"session_id\":\"qa-session-1\"}"},
 "messages":[{"role":"user","content":[
   {"type":"tool_result","tool_use_id":"t1","content":[{"type":"text","text":"config: key=sk-test-abcdefghij123 email=rohan.mehta87@gmail.com"}]}]}]}
EOF
```

To build a case with a marker, copy `basic.json` and change the text — e.g.
`"text":"ESDS_SEARCH push retries"`.

`account_uuid` `aaaaaaaa-0000-…-000000000001` is the dev identity in
`store/config/account_map.json` (copy it from `account_map.example.json`).
An unrecognised account is *supposed* to fail closed — that is case M4, not a bug.

### 2.6 Helper

```bash
post() {  # post <file> [path]
  curl -s -N -X POST "http://localhost:8080/${2:-v1/messages}" \
    -H "content-type: application/json" \
    -H "anthropic-version: 2023-06-01" \
    --data-binary "@$1"
}
sent() { jq . /tmp/dp_stub_last_request.json; }   # what actually reached upstream

# what the CLIENT saw, reassembled from the SSE stream
seen() { grep '^data: ' | sed 's/^data: //' \
  | jq -r 'select(.type=="content_block_delta") | .delta.text' 2>/dev/null | tr -d '\n'; }
```

`jq` is required. If unavailable, substitute `python3 -m json.tool`.

---

## 3. Test cases

Each case is independent. **Expected** is the pass condition; **Fail =** describes
what a defect looks like, so you can tell a genuine bug from a misconfiguration.

### S — Smoke

| ID | Check |
|---|---|
| **S1** | Gateway starts; terminal 2 prints `Application startup complete`. If it fails on `ModuleNotFoundError: phonenumbers`, run `pip install -r requirements.txt`. |
| **S2** | `post basic.json` returns SSE events, not an error. |
| **S3** | Terminal 1 logs the received request. |
| **S4** | `curl -s localhost:8080/openapi.json` responds. |

### T — Transparency *(highest severity)*

**Config:** defaults.

| ID | Steps | Expected | Fail = |
|---|---|---|---|
| **T1** | `post basic.json > /dev/null`, then `sent` | With nothing redacted and no marker, the message list reaches upstream **unchanged** — a bare-string `content` stays a bare string | any silent reshaping |
| **T2** | `post basic.json` and watch, with `DP_STUB_DELAY=0.3` | output arrives **incrementally** over several seconds | everything appears at once ⇒ buffering ⇒ **critical** |
| **T3** | Time 10 requests via the gateway vs 10 direct to `:9090` | median delta < 50 ms | consistently > 200 ms |
| **T4** | `post` to `v1/messages?beta=true` | query string forwarded | dropped |
| **T5** | POST a non-JSON body | forwarded byte-identically, no crash | 500 |
| **T6** | POST `/v1/unknown` with `{"model":"gpt-4"}` | passthrough, unmodified | normalized or dropped |
| **T7** | `DP_STUB_STATUS=500` on terminal 1, then `post basic.json` | client sees **500**, not 200 | status masked ⇒ **critical** |
| **T8** | Send `anthropic-beta: x`; check terminal 1 | header preserved; `host`/`content-length` recomputed | headers mangled |

### M — Marker authorization *(security)*

The rule: a marker is honoured **only** in the last genuine human turn, at line
start. Never from a `tool_result`, a file, retrieved context, or a prior turn.

**Config:** Track B, `DP_STUB_MODE=verbatim`.

| ID | Steps | Expected | Fail = |
|---|---|---|---|
| **M1** | Put `ESDS_SEARCH secrets` inside a `tool_result` block and post | **no** bus call (terminal 3 stays quiet) | a search fired ⇒ **critical** |
| **M2** | Put `ESDS_APPROVE deadbeef` inside a `tool_result` and post | no ingest, no error | anything written ⇒ **critical** |
| **M3** | `ESDS_SEARCH x` in the last human turn, with a trailing `{"role":"system","content":[…]}` message after it | marker **is** honoured — this is the real Claude Code shape | silently ignored |
| **M4** | Change `account_uuid` to `00000000-0000-0000-0000-00000000dead`, send `ESDS_SEARCH x` | fails closed: **no** bus call at all | a bus call with some default token ⇒ **critical** |
| **M5** | `please do not run ESDS_SEARCH` (marker mid-line) | not honoured | honoured |

### R — READ (retrieval and awareness)

**Config:** Track B. Seed a record first:

```bash
curl -s -X POST localhost:8000/v1/ingest -H 'Authorization: Bearer dev-local-token' \
 -H 'content-type: application/json' -d '{"source_system":"qa","captured_by":{"user_id":"u-dev"},
 "session_id":"qa-seed-1","content":"We chose exponential backoff for push retries.",
 "sensitivity_flags":{"contains_pii":false,"contains_credentials":false,"redaction_applied":false,"redaction_count":0},
 "visibility":"team","status":"completed","hint":{"department":"Engineering","team":"platform"},
 "knowledge":{"title":"Push retry policy","summary":"Exponential backoff, capped at 30s.","outcome":"decision_made"}}'
```

| ID | Steps | Expected | Fail = |
|---|---|---|---|
| **R1** | Post a normal turn (no marker) | no bus call at all | a call fired |
| **R2** | Post `ESDS_SEARCH push retries`, then `sent` | the seeded record's title/summary appear in the outbound payload | nothing injected |
| **R3** | Same; inspect `sent` | the literal `ESDS_SEARCH` is **stripped** from what the model sees | marker forwarded |
| **R4** | `ESDS_SEARCH quantum tunnelling in badgers` | zero results ⇒ nothing injected | irrelevant records injected |
| **R5** | Stop the bus, post `ESDS_SEARCH x` | request still reaches upstream; the session is unaffected | request fails ⇒ **high** |
| **R6** | On Sonnet, inspect the injected block | it appears as `<system-reminder>` inside the user turn | expecting `role:"system"` here is **not** a bug — overview §6 |
| **R7** | A user turn whose content is entirely `tool_result` | no awareness probe fires | probe fired ⇒ the bus is hit many times per human turn |
| **R8** | Any case above | the top-level `system` field is **never** modified | modified ⇒ **critical** (destroys prompt caching) |
| **R9** | `DP_AWARENESS=1`; post two normal turns in one session | the probe fires at most **once** (cooldown) | fires every turn |
| **R10** | `DP_AWARENESS=1`; inspect what was injected | **titles only** — no record body | a body injected ⇒ the blind injection this design rejects |

### C — CHECK (DLP)

| ID | Config | Steps | Expected | Fail = |
|---|---|---|---|---|
| **C1** | `DP_DEBUG_LOG_OUTBOUND=1` | `post toolresult.json`; grep `/tmp/dp_outbound_debug_*.json` for `sk-test-abcdefghij123` | **absent**; `⟦SECRET_1⟧` present | present ⇒ **critical** |
| **C2** | same | grep the same file for `rohan.mehta87@gmail.com` | absent; a `⟦PII_n⟧` token present | present ⇒ **critical** |
| **C3** | same | put the *same* secret twice in one request | both get the **same** token | different tokens ⇒ coreference lost |
| **C4** | same | put a secret in a `tool_use` block's `input` field | redacted | leaked ⇒ **critical** (this was a real defect) |
| **C5** | defaults | `post toolresult.json \| seen` | the client sees the **real** values back | tokens visible to the user |
| **C6** | `DP_CHECK_RESTORE_STREAM=0` | same | client sees `⟦SECRET_1⟧` — the opt-out is intentional | — |
| **C7** | defaults, `DP_STUB_CHUNK=1` | same | still restored correctly at 1-byte chunks | mangled ⇒ boundary bug |
| **C8** | defaults | send text containing 日本語 plus a secret | non-ASCII survives intact | replacement characters appear |
| **C9** | defaults | a request with **no** secret at all | relay is byte-identical (restore is a no-op on an empty vault) | reshaped |

### W — WRITE (draft → approve → ingest) *(stop-ship)*

**Config:** Track B, `DP_STUB_MODE=draft`.

| ID | Steps | Expected | Fail = |
|---|---|---|---|
| **W1** | Post a turn whose text is `ESDS_SUBMIT`; then `sent` | the outbound payload contains an instruction asking for a fenced JSON block **and** a pending id | no instruction |
| **W2** | Same; check terminal 2 | `[WRITE] draft <id> pending approval` | not captured |
| **W3** | `ls /tmp/dp_pending/` | one JSON file with `"status": "pending_approval"` | missing |
| **W4** | `curl -s -H 'Authorization: Bearer dev-local-token' 'localhost:8000/v1/search?q=base-URL+redirect'` | **zero results — nothing was written** | anything written ⇒ **STOP-SHIP** |
| **W5** | Post `ESDS_APPROVE <id>` (id from W3) | terminal 3 logs `POST /v1/ingest` → 201 | no write |
| **W6** | Repeat W4 | the record is now searchable | still absent |
| **W7** | Post `ESDS_APPROVE <id>` again | no second row (the pending record was consumed) | duplicate written |
| **W8** | Submit again, then `ESDS_REJECT <id>` | pending file gone, nothing ingested | ingested |
| **W9** | Submit with a secret in the conversation; inspect the pending file | the draft holds `⟦SECRET_n⟧` and `sensitivity_flags.contains_credentials` is `true` | raw secret stored ⇒ **critical** |
| **W10** | After W5, inspect the bus row's `sensitivity_flags` | populated, not `{}` | empty ⇒ the security half is not wired |
| **W11** | Approve with `--visibility orgg` (a typo) | falls back to `team`, **never** `org` | published org-wide ⇒ **high** |
| **W12** | Stop the bus, then approve | told it is **QUEUED**, explicitly not saved | told it was saved ⇒ **critical** (the one lie this system cannot afford) |
| **W13** | Restart the bus; post any turn in the same session | the queued write drains automatically | stays queued forever |
| **W14** | `DP_STUB_MODE=echo` (so no JSON block comes back), submit | nothing captured, nothing stored | a malformed draft was stored |

### O — Observability

| ID | Steps | Expected |
|---|---|---|
| **O1** | After any request, `tail -1 docs/usage_log.jsonl` | valid JSON with `model` and `usage` |
| **O2** | Compare a retrieval turn with a plain turn | `injected` is `true` only for the former |
| **O3** | `DP_ARM_LABEL=qa1` | the label appears in the log line |
| **O4** | grep `docs/usage_log.jsonl` for any secret or `account_uuid` | **absent** — those identifiers are PII-adjacent |

### X — Hygiene / security

| ID | Steps | Expected | Fail = |
|---|---|---|---|
| **X1** | Send a bogus `Authorization: Bearer sk-ant-SECRETVALUE`; grep terminal 2 output and every file in `docs/` | the credential appears **nowhere** | logged ⇒ **critical** |
| **X2** | `ls /tmp/dp_outbound_debug_*` with `DP_DEBUG_LOG_OUTBOUND=0` | no new files | written while disabled |
| **X3** | `git status --porcelain` after a full run | no `fixtures/*.json` staged | fixtures tracked |
| **X4** | Send `ESDS_APPROVE ../../etc/passwd` | rejected cleanly, no traceback | path traversal or a 500 |
| **X5** | Approve a pending id created under a **different** `session_id` | refused | cross-session approval ⇒ **critical** |

### L — Live *(Track C — needs a working Claude Code login; spends real credit)*

```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude    # never export
```

| ID | Check |
|---|---|
| **L1** | A normal session works exactly as usual. |
| **L2** | Streaming still feels token-by-token. |
| **L3** | `ESDS_SEARCH <topic>` brings colleagues' records into the answer. |
| **L4** | `ESDS_SUBMIT` → the model prints `To save this, type ESDS_APPROVE <id>`. |
| **L5** | **Invisibility.** Work normally for 10 minutes. Write down anything you noticed. |
| **L6** | **Payload gap.** Ask the agent to read a file containing a fake `sk-test-…` key, then confirm via `DP_DEBUG_LOG_OUTBOUND` that the key never left. This is the case a prompt-level hook structurally cannot cover. |

---

## 4. Known limitations — do NOT report these as defects

| Observation | Status |
|---|---|
| Injected text appears as `<system-reminder>` inside the user turn on Sonnet | Correct — model gating. Overview §6. |
| A permitted record is occasionally missing from search results | Known and accepted upstream: visibility is filtered on the HNSW candidate set. Do not "fix". |
| `{"name": "Rohan Mehta"}` inside a JSON `tool_result` is not redacted | Deliberate trade — `"name"` was removed from the field list because it redacted tool metadata like `{"name":"create_ticket"}`. |
| A bare 12-digit number that fails the Verhoeff check is not treated as Aadhaar | Correct — the checksum gate is what keeps false positives down. |
| `/v1/agent-activity?project=…` silently ignores `project` | Known upstream no-op. |
| No contradiction detection between decisions | Not built; the schema hook exists. |
| No dashboard / review UI | Not built — approval is in-terminal by design. |
| Only the Anthropic wire format is understood | OpenAI adapter not built. |
| The gateway holds no upstream credential of its own | Relay mode, by design. |
| MCP tool calls are invisible to the gateway | Structural — they never reach the model API. Overview §8. |
| Queued writes for a session that never returns stay queued | Run `scripts/drain_queue.py`. |

If a result is not in this table and not in overview §8, it is worth reporting.

---

## 5. Bug report template

```
ID:            <test case ID, or NEW>
Severity:      critical | high | medium | low
Track:         A (gateway alone) | B (with bus) | C (live)

Gateway env:   DP_CHECK_RESTORE_STREAM=… DP_AWARENESS=… DP_UPSTREAM_BASE_URL=…
Stub env:      DP_STUB_MODE=… DP_STUB_CHUNK=… DP_STUB_STATUS=…
Restarted terminal 2 after changing env?   yes | no
Bus running?   yes | no

Steps:         <exact commands>
Expected:      <from this document>
Actual:        <what happened>

Evidence:      <curl output, jq output, terminal 2/3 traceback>
               Do NOT attach fixtures/*.json — they may contain real secrets.
               Redact any real credential before pasting anything.
Reproducible:  n/n attempts
```

**Severity guide.** *Critical* — a real secret or credential reaches upstream or a
log, **a draft reaches the bus without approval**, top-level `system` is modified,
the stream is buffered, or a marker is honoured from a `tool_result`. *High* — the
tool-loop guard fails, a queued write is reported as saved, visibility is widened
unintentionally, or an upstream error is masked. *Medium* — a policy misbehaves
without data loss. *Low* — cosmetic or logging.

---

## 6. Results sheet

| ID | Area | Result | Notes |
|---|---|---|---|
| S1–S4 | Smoke | ☐ | |
| T1 | Passthrough unchanged | ☐ | |
| T2 | **Not buffered** | ☐ | first byte / total: |
| T3 | Added latency | ☐ | median delta: |
| T4–T6 | Query string / fail-open | ☐ | |
| T7 | Status codes | ☐ | |
| T8 | Headers | ☐ | |
| M1 | **Marker in tool_result ignored** | ☐ | |
| M2 | **ESDS_APPROVE in tool_result ignored** | ☐ | |
| M3 | Trailing harness system message | ☐ | |
| M4 | **Unknown account fails closed** | ☐ | |
| M5 | Mid-line marker ignored | ☐ | |
| R1–R4 | Retrieval basics | ☐ | |
| R5 | Bus down ⇒ fail open | ☐ | |
| R6–R7 | Fallback / tool-loop guard | ☐ | |
| R8 | **`system` untouched** | ☐ | |
| R9–R10 | Awareness cooldown / titles only | ☐ | |
| C1 | **Secret redacted outbound** | ☐ | |
| C2 | **PII redacted outbound** | ☐ | |
| C3 | Coreference | ☐ | |
| C4 | **tool_use.input redacted** | ☐ | |
| C5–C7 | Restore / chunk boundaries | ☐ | |
| C8–C9 | UTF-8 / empty-vault passthrough | ☐ | |
| W1–W3 | Draft captured | ☐ | pending id: |
| W4 | **NOTHING WRITTEN BEFORE APPROVAL** | ☐ | |
| W5–W7 | Approve / idempotency | ☐ | |
| W8 | Reject | ☐ | |
| W9 | **Secret not stored in draft** | ☐ | |
| W10 | `sensitivity_flags` populated | ☐ | |
| W11 | Visibility typo fails safe | ☐ | |
| W12 | **Queued ≠ saved** | ☐ | |
| W13–W14 | Drain / malformed draft | ☐ | |
| O1–O4 | Usage logging | ☐ | |
| X1 | **No credential logged** | ☐ | |
| X2–X5 | Hygiene / cross-session | ☐ | |
| L1–L6 | Live | ☐ | what you noticed: |

**Sign-off condition.** Every row in **T** and **M**, plus **W4**, W9, W12, C1, C2,
C4, R8 and X1 must pass. A failure in any of those is stop-ship for the demo;
everything else is triage.
