# T0/T1/T2 wire findings

Status: gateway code built and validated mechanically against a local stub
backend, PLUS real T0 captures now exist in `fixtures/` (3 bodies, captured
2026-08-08 13:33–13:53 via a live Claude Code session pointed at
`gateway/tap.py`). This section is now split into: real findings from those
captures, mechanical findings from stub validation, and what's still open.

**Note:** the mechanics below (non-buffering, injection, tool-loop guard,
usage extraction) were re-homed 2026-08-08 into a protocol-adapter
architecture — `gateway/protocol/anthropic_adapter.py` (wire-format
details, including the model-gating fallback for `role:"system"`) and
`gateway/policies/read.py` (the turn-detection and injection semantics).
Behavior is unchanged; only where the code lives changed. `ARCHITECTURE.md`
itself is unaffected — this is a `gateway/` internal restructuring, not a
design change.

## Real findings (from fixtures/2026*.json, 2026-08-08)

- **Model confirmed: `claude-sonnet-5`** on all three real captures. This is
  the exact model ARCHITECTURE.md §2.2 and the Claude API reference both
  list as NOT supporting mid-conversation `role:"system"` — directly
  explains the earlier "T2 failed on the real Anthropic API" bug report.
- **`cache_control` breakpoint count, answered:** the third fixture (a
  2-message conversation) shows **3 total `cache_control` occurrences**,
  2 of them inside the top-level `system` array (on the "You are Claude
  Code..." block and the long tools/instructions block, both with
  `ttl: "1h"`). That leaves at most 1 free breakpoint of the API's 4-max
  budget on a short conversation like this — on a longer agentic turn with
  more of the harness's own breakpoints in play, assume close to zero free
  slots. §2.2a's "have the gateway place its own breakpoint" fallback
  should not be assumed available without checking on every request.
- **`messages[].content` shape confirmed both ways** on real traffic: a
  bare string on the short single-turn fixtures, a list-of-blocks on the
  longer one. The normalization in `AnthropicAdapter.to_normalized`
  (string → single text block) matches this correctly.
- **Surprising finding, not yet resolved:** the third fixture's last
  message has `role: "system"` — but this did NOT come from my gateway's
  injection. `tap.py` never forwards or mutates anything; this is **Claude
  Code's own harness** natively constructing a mid-conversation
  `role:"system"` message (containing its own agent-list/skills/hook-output
  content) as part of its normal request construction, independent of any
  gateway. Since `tap.py` never forwards to the real API, this fixture
  proves Claude Code is *willing* to send that shape — it does NOT prove
  whether the real API accepts or rejects it for `claude-sonnet-5`.
  **Next concrete step:** run the same kind of prompt live through
  `gateway/app.py` (not `tap.py`) with `DP_INJECT=0` — since this
  particular system message isn't from injection at all — and watch
  whether Claude Code's own request 400s against the real API on its own,
  independent of anything this gateway does. That would directly settle
  whether the model-gating restriction is as absolute as documented, or
  whether Claude Code has its own handling this hasn't accounted for.
- No `tool_result` block in any of the three real captures yet — T3 (the
  payload-gap proof) still needs a dedicated real run.

## Confirmed mechanically (stub validation, 2026-08-08)

- **`ANTHROPIC_BASE_URL` redirect mechanism**: not yet confirmed against a
  real Claude Code process — this is the first thing to check live (see
  "Needs your terminal" below). Everything downstream assumes this works.
- **Streaming is not buffered.** Direct-to-stub: first byte `0.0017s`, total
  `1.805s`. Through gateway: first byte `0.0022s`, total `1.808s`. Added
  latency ≈ 0.5ms; the arrival pacing is identical. Measured against a stub
  emitting 6 chunks at 0.3s intervals — if the gateway buffered, first-byte
  time would equal total time (~1.8s) instead of ~2ms.
- **Non-streaming passthrough is byte-identical.** Request body forwarded
  unmodified when `DP_INJECT=0`; response relayed unmodified.
- **Mutation (T2) lands correctly.** With `DP_INJECT=1`, a genuine human
  turn gets a `{"role": "system", "content": "..."}` message appended to the
  tail of `messages[]` — top-level `system` is never touched, matching
  ARCHITECTURE.md §2.2's requirement to preserve the cache prefix.
- **The tool-loop guard works.** A user turn whose `content` is entirely
  `tool_result` blocks is correctly *not* injected into — verified against
  both a genuine question and a synthetic tool-loop hop. Without this guard,
  the same context would repeat on every hop of a multi-step tool call.
- **`content` shape handling.** `messages[].content` as both a bare string
  and a list-of-blocks were exercised in synthetic fixtures; injection
  normalizes correctly in both cases (append after the last message, always
  as a new `{"role":"system",...}` entry, so no in-place string mutation is
  needed).
- **`tool_result` is where a planted secret lands**, confirmed via a
  synthetic fixture: a `config.py` containing an AWS-shaped key, read via a
  simulated `Read` tool call, produces the key inside
  `messages[-1].content[0].content` (a `tool_result` block) — not in any
  user-authored text. This is the concrete case for T3, reproduced
  mechanically; needs one real capture to confirm Claude Code's actual
  tool-result JSON shape matches (see below).

## Needs your terminal (cannot be produced synthetically)

Run, in a second terminal — **do not `export` this into any shell also used
for interactive Claude Code work on other projects**, since it will redirect
ALL of that shell's Claude Code traffic to the local gateway for as long as
it's set:

```bash
# Terminal A
cd /home/sahaj/Projects/hackathon_agent_layer
.venv/bin/uvicorn gateway.tap:app --port 8080

# Terminal B — prefix-scoped, not exported
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

Drive it three ways (per TEST-PLAN.md T0): (a) a one-line question, (b) a
question that makes it read a file, (c) a question that makes it run a bash
command. Then answer:

1. **Auth header — still open.** `tap.py` only persists the request BODY to
   `fixtures/`, not headers (those only went to the console, which wasn't
   captured). ARCHITECTURE.md §2.0 claims both API-key and OAuth modes are
   live on this machine; re-run tap.py and note the `auth mode` line it
   prints. *(Doesn't block anything — `app.py` runs in relay mode and
   forwards whichever header arrives unmodified — but a production `dp_*`
   key scheme needs to know which one to expect.)*
2. ~~How many `cache_control` breakpoints does Claude Code already place~~
   — **answered above: 3 total, 2 in `system` blocks**, on a short
   conversation. Re-check on a long agentic turn before assuming the
   remaining budget holds — more of the harness's own content may earn
   more breakpoints as the conversation grows.
3. **Largest block count in a single turn** — the longest real capture so
   far shows 8 blocks in the injected `role:"system"`-adjacent turn, well
   under the 20-block lookback threshold in §2.2a. This is NOT yet a real
   stress test — none of the three captures involve actual tool use or
   parallel tool calls. Still needs a genuinely agentic multi-tool-call
   turn to mean anything for §2.2a.
4. **Real `tool_result` shape — still open.** None of the three real
   captures contain one yet. Repeat T3 for real: create a file with an
   AWS-shaped key, ask Claude Code what's wrong with it (through
   `gateway/tap.py`), then `grep -r AKIA fixtures/` and confirm it appears
   inside a `tool_result` block and nowhere in user-authored text.
5. **Stable conversation identifier — still open.** None of the three
   captures show an obvious session key (relevant to ARCHITECTURE.md open
   question #2 — `passport_usage` attribution wants one; the
   `<orgbrain injected=...>` marker in §2.3 sidesteps this for
   injection state specifically, but doesn't solve it generally).
6. **Does Claude Code's own `role:"system"` usage survive against the real
   API on `claude-sonnet-5`?** New question, raised by the "surprising
   finding" above — not the same as testing gateway injection, since this
   system message comes from Claude Code itself, not from `DP_INJECT`. Run
   a similar prompt through `gateway/app.py` (real API, `DP_INJECT=0`) and
   see whether Claude Code's own request 400s on its own.
7. **T1 behavioral pass — still open, judgment only.** 10 minutes of real
   work through `gateway/app.py` (real API, `DP_INJECT=0`). Tokens should
   appear progressively, tool calls should complete, Ctrl-C should
   interrupt cleanly, and it should feel invisible. Nothing above
   substitutes for this — it's the one item that can't be a number.

## T5 — second-harness choice

Deferred until the above fixtures exist, per TEST-PLAN.md. Decide after
looking at what a second harness's request body actually looks like:

- OpenAI-format client (Cursor / Continue) → proves genuine wire-format
  agnosticism, needs a `/v1/chat/completions` route + a second text-slot
  walker (~3 hrs, Day 2 work).
- Anthropic-native client (Aider, or a bare SDK script) → proves the
  env-var mechanism generalizes past Claude Code, zero new gateway code.

Record the choice and why here once made.
