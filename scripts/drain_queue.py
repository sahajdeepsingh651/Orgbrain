"""Drain writes that were queued while the Context Bus was unreachable.

The gateway retries queued writes opportunistically on the next request
from the same session (G8). That covers the normal case — the developer
whose write it is keeps working. It does NOT cover a session that never
comes back: a developer who queued a write and then closed their terminal.

This script is the manual sweep for that case. It drains EVERY queued
write across all sessions.

    .venv/bin/python scripts/drain_queue.py            # dry run, list only
    .venv/bin/python scripts/drain_queue.py --commit   # actually retry

Requires the Context Bus reachable at DP_BUS_BASE_URL and the identity map
at DP_IDENTITY_MAP (default store/config/account_map.json).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import bus_client, flows, pending  # noqa: E402


async def main() -> int:
    commit = "--commit" in sys.argv
    queued = pending.list_queued()
    if not queued:
        print("nothing queued")
        return 0

    print(f"{len(queued)} queued write(s) in {pending.PENDING_DIR}:")
    for record in queued:
        k = (record.get("draft") or {}).get("knowledge") or {}
        print(f"  {record['pending_id']}  session={record.get('session_id')}  "
              f"title={k.get('title', '?')!r}  last_error={record.get('last_error')!r}")

    if not commit:
        print("\ndry run — pass --commit to retry these")
        return 0

    sessions: dict[str, str | None] = {}
    for record in queued:
        sessions.setdefault(record.get("session_id"), record.get("account_uuid"))

    failed = 0
    try:
        for session_id, account_uuid in sessions.items():
            for result in await flows.drain_queued_writes(session_id, account_uuid):
                if result["ok"]:
                    print(f"  OK   {result['pending_id']} -> {result['record_id']}")
                else:
                    failed += 1
                    print(f"  FAIL {result['pending_id']}: {result['reason']}")
    finally:
        await bus_client.aclose()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
