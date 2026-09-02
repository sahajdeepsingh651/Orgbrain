#!/usr/bin/env bash
# T4 — Measurement A: prompt cache cost of injection, driven by your own
# Claude Code subscription (no API key, no cost outside your existing plan).
#
# RUN THIS YOURSELF, in your own terminal — not delegated to an assistant
# session, since it spends real turns against your subscription and spawns
# a nested `claude` process. Read it before running; it is not long.
#
# What it does:
#   1. Starts the gateway pointed at the REAL Anthropic API (not a stub),
#      with DP_INJECT=0.
#   2. Drives a fixed 3-turn conversation via `claude -p` / `claude -c -p`,
#      through ANTHROPIC_BASE_URL=http://localhost:8080, in a scratch dir
#      seeded with this repo's docs (so there's real file-reading to do).
#   3. Restarts the gateway with DP_INJECT=1 and the placeholder passport
#      text as the injected block.
#   4. Repeats the SAME conversation as a fresh session.
#   5. Builds docs/MEASUREMENT-A.md from the resulting docs/usage_log.jsonl.
#
# Uses --dangerously-skip-permissions so the non-interactive `-p` calls
# don't hang on a tool-approval prompt. The conversation only reads files
# and runs `ls`/`grep` inside a scratch copy of this repo's docs — nothing
# destructive. Remove that flag and approve manually if you'd rather watch
# each tool call.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv/bin"
SCRATCH="$(mktemp -d)"
PORT=8080
GATEWAY_URL="http://localhost:${PORT}"

cleanup() {
  [[ -n "${GW_PID:-}" ]] && kill "$GW_PID" 2>/dev/null || true
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

cp "$ROOT/docs/ARCHITECTURE.md" "$SCRATCH/"
cp "$ROOT/docs/submissions/approach.md" "$SCRATCH/SUBMISSION-approach.md"
cp "$ROOT/docs/submissions/infrastructure.md" "$SCRATCH/SUBMISSION-infrastructure.md"
cp "$ROOT/docs/TEST-PLAN.md" "$SCRATCH/"

start_gateway() {
  local inject="$1" arm_label="$2" inject_text="${3:-}"
  DP_INJECT="$inject" DP_ARM_LABEL="$arm_label" DP_INJECT_TEXT="$inject_text" \
    "$VENV/uvicorn" gateway.app:app --app-dir "$ROOT" --port "$PORT" --log-level warning &
  GW_PID=$!
  # Wait for the port to accept TCP connections — a raw connect check, not
  # an HTTP request, so this never triggers a real (if unauthenticated and
  # harmless) call to the upstream API during startup.
  for _ in $(seq 1 30); do
    if (exec 3<>"/dev/tcp/localhost/${PORT}") 2>/dev/null; then
      exec 3>&- 3<&-
      break
    fi
    sleep 0.2
  done
}

stop_gateway() {
  kill "$GW_PID" 2>/dev/null || true
  wait "$GW_PID" 2>/dev/null || true
  GW_PID=""
}

run_conversation() {
  ( cd "$SCRATCH" && \
    ANTHROPIC_BASE_URL="$GATEWAY_URL" "$(command -v claude)" \
      -p --dangerously-skip-permissions \
      "Read ARCHITECTURE.md, SUBMISSION-approach.md, SUBMISSION-infrastructure.md, and TEST-PLAN.md, using parallel tool calls where possible. Summarize each in one sentence." \
      > /dev/null

    ANTHROPIC_BASE_URL="$GATEWAY_URL" "$(command -v claude)" \
      -c -p --dangerously-skip-permissions \
      "Run 'ls -la' and grep for the word gateway across all the markdown files you have access to." \
      > /dev/null

    ANTHROPIC_BASE_URL="$GATEWAY_URL" "$(command -v claude)" \
      -c -p --dangerously-skip-permissions \
      "Based on everything so far, what is the single most important design decision in this project, in one sentence?" \
      > /dev/null
  )
}

echo "=== Arm A: no injection ==="
start_gateway 0 arm_a_no_inject
run_conversation
stop_gateway

echo "=== Arm B: with injection ==="
INJECT_TEXT="$(cat "$ROOT/fixtures/placeholder_passport.txt")"
start_gateway 1 arm_b_inject "$INJECT_TEXT"
run_conversation
stop_gateway

echo "=== Building table ==="
"$VENV/python" "$ROOT/scripts/build_measurement_table.py"

echo
echo "Done. See docs/MEASUREMENT-A.md"
