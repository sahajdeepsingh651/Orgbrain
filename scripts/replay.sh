#!/usr/bin/env bash
# Replay a captured fixture at the gateway, for fast iteration.
#
# Round-tripping through a live Claude Code session is ~15s; this is ~200ms.
#
# Usage:
#   scripts/replay.sh fixtures/20260808T124609_v1_messages.json
#   scripts/replay.sh fixtures/some_fixture.json http://localhost:8080

set -euo pipefail

FIXTURE="${1:?usage: replay.sh <fixture.json> [gateway_url]}"
GATEWAY="${2:-http://localhost:8080}"

if [[ ! -f "$FIXTURE" ]]; then
  echo "fixture not found: $FIXTURE" >&2
  exit 1
fi

curl -s -N -X POST "$GATEWAY/v1/messages" \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  --data-binary "@$FIXTURE" \
  -w "\n[replay] http_code=%{http_code} first_byte=%{time_starttransfer}s total=%{time_total}s\n"
