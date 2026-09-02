# Test plan — validate the gateway interception thesis

## Context

`ARCHITECTURE.md` commits the whole project to one mechanism: a reverse proxy in front of
`/v1/messages`, reached by setting two environment variables in the developer's existing
harness. §0 states the bet plainly — *"a gateway is the only mechanism that is both push and
harness-agnostic"* — and every other decision in the document (identity in §2.0, injection
point in §2.2, DLP restore in §5, the tee in §4.1) assumes that bet pays off.

Nothing has been built. The repo contains three markdown files and no code.

Two things can still invalidate the bet, and both are cheap to measure **now** and expensive
to discover on day 3:

1. **Does proxying feel invisible?** If streaming stutters or first-token latency is
   visible, developers unset the env var and participation goes to zero. The product dies
   at adoption, not at architecture.
2. **What does injection cost in prompt cache?** §9 flags this as Measurement A and says
   explicitly: if the delta is bad you want two days to fix it with breakpoint management
   (§2.2a), not zero.

This plan builds only enough gateway to answer those two questions, plus three cheap proofs
that become demo beats. **It is a validation ladder, not the product.** Each rung is
falsifiable and has a defined failure action.

### Deliberately out of scope

No Postgres, no pgvector, no embeddings, no retrieval, no DLP detectors, no extractor, no
review queue, no auth beyond a hardcoded key map. Those are Day 1–2 build work in §9 and
none of them can change the *decision* this plan exists to test.

---

## Environment

Python 3.11+, `fastapi`, `uvicorn`, `httpx`, `anthropic`. Anthropic API key available as
`DP_UPSTREAM_KEY` in the shell (never hardcoded, never logged).

## Layout to create

```
gateway/
  tap.py               # T0 — log-only, no forwarding
  app.py               # T1–T4 — passthrough, then mutation, then measurement
fixtures/              # captured real request bodies (gitignored if any are sensitive)
scripts/
  replay.sh            # POST a fixture at the gateway, for fast iteration
  measure_cache.py     # T4 — drive a scripted conversation, tabulate usage
docs/
  WIRE-FINDINGS.md     # T0 deliverable
  MEASUREMENT-A.md     # T4 deliverable
```

---

## T0 — The tap (~30 min)

Prove the redirect happens at all, and capture real bodies before designing against
imagined ones.

**Build** `gateway/tap.py`: a FastAPI app with a single `POST /{path:path}` route that
writes the raw body to `fixtures/<timestamp>.json`, prints a truncated pretty version, and
returns `{"error": {"type": "tap", "message": "intercepted"}}`. It does not forward. Claude
Code will error — expected.

**Run**

```bash
uvicorn gateway.tap:app --port 8080
# second terminal
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

Drive it three ways: (a) a one-line question, (b) a question that makes it read a file,
(c) a question that makes it run a bash command.

**Pass:** ≥3 distinct bodies captured, at least one containing a `tool_result` block.

**Deliverable — `docs/WIRE-FINDINGS.md`,** answering these specifically. Every one of them
is a design input that §2 currently assumes rather than knows:

- Which auth header arrives — `x-api-key: sk-ant-…` or `Authorization: Bearer sk-ant-oat01…`
  plus `anthropic-beta: oauth-2025-04-20`? §2.0 claims both modes are live; confirm which
  one *this* machine sends.
- Is `messages[].content` a bare string or a list of blocks? Both appear; the injection
  helper must normalize.
- **How many `cache_control` breakpoints does Claude Code already place, and where?** The
  API allows 4 total. §2.2a's breakpoint-management fallback is only available if there is
  a free slot. This is the single most important line in the findings doc.
- How many content blocks appear in the largest single turn? §2.2a's 20-block lookback
  concern is only real above that threshold.
- Does anything in the request carry a stable conversation identifier? (Open question #2.)

**Fail action:** if `ANTHROPIC_BASE_URL` is ignored, stop the whole plan and report — the
architecture's central assumption is wrong and hooks become the fallback.

---

## T1 — Transparent passthrough (~2–3 hrs)

**Build** `gateway/app.py`: forward to `https://api.anthropic.com`, return the response
untouched.

Requirements an implementer gets wrong:

- **Never buffer the stream.** `yield` each chunk inside the `async for`, not after it.
  Use `httpx.AsyncClient.stream(...)` + `aiter_raw()` + `StreamingResponse(media_type="text/event-stream")`.
- Strip hop-by-hop headers before forwarding: `host`, `content-length`, `connection`,
  `accept-encoding`.
- Replace the inbound credential with `DP_UPSTREAM_KEY`. Reject inbound `sk-ant-*` with a
  clear enrolment error (§2.0 keeps this for the demo). Accept a hardcoded `dp_test_*`.
- Timeout must be generous — `httpx.Timeout(600.0, connect=10.0)`. The default will kill
  long agentic turns.
- The upstream key must never reach a log line.

**Pass — behavioural, not technical:**

1. Use Claude Code through the gateway for 10 minutes on real work and forget it is there.
2. Tokens appear progressively, not in one burst at the end.
3. Multi-step tool use completes; Ctrl-C interrupts cleanly.
4. Added time-to-first-byte < 50 ms vs. direct. Measure with a fixed prompt, five runs each.

**Fail action:** if output arrives in a burst, the stream is being buffered — check for
`await response.aread()`, a non-streaming `httpx.post`, or a middleware that reads the body.
If it still stutters after that, record it and escalate: this is the adoption-risk signal
and it justifies falling back to hooks.

Also add `scripts/replay.sh` here — `curl` a saved fixture at the gateway. Round-tripping
through Claude Code is ~15 s; a fixture replay is ~200 ms.

---

## T2 — Mutation proof (~15 min)

Append `"\n\nAlways end your reply with 🛂"` to the last user message. Normalize
string→list first.

**Pass:** the emoji appears. That is the read path proven; retrieval and ranking are
refinements on a mechanism now known to work.

**Guard to implement now, not later:** only mutate when the last message is a genuine human
turn. A user message whose content is entirely `tool_result` blocks is the agent looping,
not a person asking. Without this guard the same context gets injected on every hop of a
tool loop.

```python
def is_new_human_turn(body) -> bool:
    last = body["messages"][-1]
    if last["role"] != "user":
        return False
    c = last["content"]
    return True if isinstance(c, str) else any(b.get("type") == "text" for b in c)
```

Verify against a fixture from T0(b) — the file-reading conversation — that this returns
`False` on the tool-loop hops.

---

## T3 — Payload-gap proof (~20 min)

The proof that a prompt-level hook is insufficient, and a demo beat.

```bash
echo 'AWS_KEY = "AKIA3F7QX2MNPLKD9WZR"' > /tmp/dp_demo/config.py
# in Claude Code: "what's wrong with config.py?"
grep -r AKIA3F7QX2MNPLKD9WZR fixtures/
```

**Pass:** the key appears inside a `tool_result` block and appears nowhere in any
user-authored text. Record which JSON path it landed at in `WIRE-FINDINGS.md`.

This is the concrete answer to "why not just use a `UserPromptSubmit` hook" — the hook sees
the sentence, the wire sees the file.

---

## T4 — Measurement A: the cache baseline (~2–3 hrs)

The measurement §9 says can still change the build.

**Comparison design — read this before implementing.** §9 says the comparison must be
*gateway vs no gateway*, not against a strawman. Run both arms **through the gateway**
anyway, with injection off in arm A and on in arm B:

- A pure-passthrough gateway sends a **byte-identical** body upstream, so the server-side
  cache behaves identically to no gateway at all. The comparison stays honest.
- Prompt caches are scoped per credential. A true no-gateway run would use the developer's
  own key and hit a different cache namespace, making the numbers incomparable. Routing
  both arms through one upstream key is not a convenience — it is the only valid form of
  this measurement.

**Build** `scripts/measure_cache.py`: drive one *scripted, repeatable* conversation
(§9 requires realism — several turns, parallel tool calls, >20 blocks in at least one turn),
capture `usage` from each `message_delta` SSE event, and tabulate per turn:
`cache_read_input_tokens`, `cache_creation_input_tokens`, `input_tokens`.

Run arm A (`DP_INJECT=0`), then arm B (`DP_INJECT=1`, injecting a ~1,500-token placeholder
block sized to §2.4's per-turn budget). Same conversation, same order.

**Deliverable — `docs/MEASUREMENT-A.md`:** the per-turn table for both arms, the delta, and
a one-line verdict.

**Interpretation and failure actions:**

| Result | Action |
|---|---|
| Cache reads roughly unchanged | Thesis holds. Put the table on the day-3 slide. |
| Reads collapse from turn 2 onward | Injection is landing before a breakpoint. Confirm you append to the tail of `messages[]` and never touch top-level `system`. |
| Reads degrade only in high-block turns | The §2.2a 20-block lookback. Apply the gateway's own `cache_control` breakpoint — but only if T0 found a free slot of the 4. |
| Reads never non-zero in *either* arm | The measurement is broken, not the design. The prompt is likely under the cacheable minimum, or something dynamic sits in the prefix. Fix before drawing conclusions. |

---

## T5 — Second-harness decision gate (~30 min of judgement)

Deferred deliberately until T0 evidence exists. Read the captured fixtures, then choose:

- **If** you want to prove genuine wire-format agnosticism (the actual pitch claim), pick an
  OpenAI-format client (Cursor / Continue) and schedule `/v1/chat/completions` + a second
  text-slot walker — roughly 3 hrs, best done Day 2 after DLP is moving.
- **If** the goal today is only to show the env-var mechanism generalizes beyond Claude
  Code, an Anthropic-native client (Aider, or a 10-line Anthropic SDK script) needs zero
  new code — but be honest on stage that it proves reach, not format handling.

Record the choice and its reasoning in `WIRE-FINDINGS.md` so day 3 does not relitigate it.

---

## Verification

The plan is complete when all of these hold:

1. `uvicorn gateway.app:app --port 8080` runs; `ANTHROPIC_BASE_URL=http://localhost:8080 claude`
   is usable for 10 minutes of real work without the operator noticing the proxy.
2. `fixtures/` contains ≥3 real bodies including one with a `tool_result` block.
3. `bash scripts/replay.sh fixtures/<name>.json` returns a valid streamed response.
4. With `DP_INJECT=1`, a Claude Code reply ends with 🛂; with `DP_INJECT=0`, it does not.
5. `grep -r AKIA3F7QX2MNPLKD9WZR fixtures/` matches inside a `tool_result` block only.
6. `docs/WIRE-FINDINGS.md` answers all five T0 questions, including the `cache_control`
   breakpoint count.
7. `docs/MEASUREMENT-A.md` contains both arms' per-turn tables and a verdict.

## Go / no-go

Proceed to the §9 Day 1 build **only if** T1 is behaviourally invisible and T4 shows no
catastrophic cache regression. If T1 fails, the adoption constraint is violated by the
mechanism itself and hooks become the fallback — a decision worth making on Day 1 rather
than discovering on Day 3.
