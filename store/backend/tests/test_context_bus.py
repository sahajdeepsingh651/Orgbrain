"""Integration test for the Context Bus write + NOTIFY (build step 3).

Prerequisites: Postgres up and the REST server running (see docs/orgbrain-setup.md).

Verifies, with a real LISTEN client (not just a DB-side row check): a committed
ingest produces a matching context_bus_events row AND fires a real NOTIFY with
matching content; a quarantined ingest produces neither; and the replay/catch-up
query (WHERE created_at > checkpoint) finds exactly the new event.
"""

import asyncio
import json
import os
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

BASE_URL = "http://127.0.0.1:8000/v1/ingest"
HEADERS = {"Authorization": "Bearer dev-local-token", "Content-Type": "application/json"}

notifications = []


def on_notify(connection, pid, channel, payload):
    notifications.append((channel, json.loads(payload)))


async def main():
    listen_conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    await listen_conn.add_listener("context_bus", on_notify)

    checkpoint = await listen_conn.fetchval("SELECT now()")

    async with httpx.AsyncClient() as client:
        happy_payload = {
            "source_system": "claude-code",
            "captured_by": {"user_id": "u-bus", "agent_id": "agent-bus"},
            "session_id": "sess-bus-1",
            "content": "testing the context bus write and notify path",
            "sensitivity_flags": {"contains_pii": False, "contains_credentials": False, "redaction_applied": False, "redaction_count": 0},
            "visibility": "team",
            "status": "completed",
            "knowledge": {"title": "Bus test entry", "summary": "Verifies context bus row + NOTIFY.", "outcome": "insight_found"},
            "hint": {"department": "Engineering", "team": "platform"},
        }
        resp = await client.post(BASE_URL, headers=HEADERS, json=happy_payload, timeout=15)
        assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"
        record_id = resp.json()["record_id"]
        print(f"1. ingest committed: record_id={record_id}")

        bad_payload = dict(happy_payload)
        bad_payload["session_id"] = "sess-bus-2-quarantined"
        bad_payload["visibility"] = "not-a-real-visibility"
        resp2 = await client.post(BASE_URL, headers=HEADERS, json=bad_payload, timeout=15)
        assert resp2.status_code == 422, f"expected 422, got {resp2.status_code}"
        print("2. quarantined request returned 422 as expected")

    await asyncio.sleep(1.5)  # give asyncpg's connection a chance to process the NOTIFY frame

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    results = {}
    try:
        row = await conn.fetchrow("SELECT * FROM context_bus_events WHERE record_id = $1", record_id)
        results["row_matches"] = (
            row is not None and row["event_type"] == "created" and row["session_id"] == "sess-bus-1"
            and row["department"] == "Engineering" and row["team"] == "platform"
            and row["agent_id"] == "agent-bus" and row["title"] == "Bus test entry"
            and row["gold_ref"] == f"/v1/knowledge/{record_id}"
        )
        print(f"3. context_bus_events row matches committed record -> {'OK' if results['row_matches'] else 'FAIL'}")

        count_bad = await conn.fetchval(
            "SELECT count(*) FROM context_bus_events WHERE session_id = 'sess-bus-2-quarantined'"
        )
        results["quarantine_produces_no_row"] = count_bad == 0
        print(f"4. quarantined request created {count_bad} bus event row(s) -> {'OK' if results['quarantine_produces_no_row'] else 'FAIL'}")

        bus_notifications = [n for ch, n in notifications if ch == "context_bus"]
        notify_ok = (
            len(bus_notifications) == 1
            and bus_notifications[0]["record_id"] == record_id
            and bus_notifications[0]["event_type"] == "created"
            and bus_notifications[0]["title"] == "Bus test entry"
            and bus_notifications[0]["gold_ref"] == f"/v1/knowledge/{record_id}"
        )
        results["notify_delivered"] = notify_ok
        print(f"5. NOTIFY delivered with matching payload ({len(bus_notifications)} received) -> {'OK' if notify_ok else 'FAIL'}")

        catchup_rows = await conn.fetch(
            "SELECT record_id FROM context_bus_events WHERE created_at > $1 ORDER BY created_at", checkpoint
        )
        catchup_ids = {str(r["record_id"]) for r in catchup_rows}
        results["replay_catchup"] = record_id in catchup_ids and len(catchup_ids) == 1
        print(f"6. replay/catch-up found exactly the new event: {catchup_ids} -> {'OK' if results['replay_catchup'] else 'FAIL'}")
    finally:
        await conn.execute("DELETE FROM context_bus_events WHERE session_id LIKE 'sess-bus-%'")
        await conn.execute("DELETE FROM knowledge_entries WHERE session_id LIKE 'sess-bus-%'")
        await conn.execute("DELETE FROM redaction_audit_log WHERE session_id LIKE 'sess-bus-%'")
        remaining = await conn.fetchval("SELECT count(*) FROM context_bus_events WHERE session_id LIKE 'sess-bus-%'")
        print(f"\ncleanup done, remaining sess-bus-* rows: {remaining}")
        await conn.close()
        await listen_conn.close()

    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    if not all(results.values()):
        raise SystemExit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
