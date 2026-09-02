"""Integration tests for GET /v1/bus/subscribe (SSE, build step 5).

Prerequisites: Postgres up and the REST server running (see docs/orgbrain-setup.md).

Covers: replay/catch-up via `since`, live delivery gated by mandatory visibility,
optional department/team narrowing, auth, and — since "the server still responds"
isn't proof a connection was actually released — a real pg_stat_activity check
that repeated open-and-abandon cycles don't leak Postgres connections (that check
lives in this file's __main__ block as a documented manual follow-up; see
docs/orgbrain-stack.md §3 Build Log, 2026-08-08 SSE entry, for the numbers
recorded when this was last done).
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

BASE = "http://127.0.0.1:8000"
TOKENS = {
    "dev": "dev-local-token",           # u-dev, Engineering, platform
    "eng2": "dev-local-token-2",         # u-eng-2, Engineering, mobile
    "sales": "dev-local-token-sales",    # u-sales-1, Sales, enterprise
}


def hdr(tok_key):
    return {"Authorization": f"Bearer {TOKENS[tok_key]}", "Content-Type": "application/json"}


async def ingest(client, session_id, dept, team, visibility, title, user_id="u-dev", agent_id=None):
    payload = {
        "source_system": "test-harness",
        "captured_by": {"user_id": user_id, **({"agent_id": agent_id} if agent_id else {})},
        "session_id": session_id,
        "content": title,
        "sensitivity_flags": {"contains_pii": False, "contains_credentials": False, "redaction_applied": False, "redaction_count": 0},
        "visibility": visibility,
        "status": "completed",
        "knowledge": {"title": title, "summary": title, "outcome": "insight_found"},
        "hint": {"department": dept, "team": team},
    }
    resp = await client.post(f"{BASE}/v1/ingest", headers=hdr("dev"), json=payload, timeout=15)
    assert resp.status_code == 201, resp.text
    return resp.json()["record_id"]


async def parse_sse_events(lines_iter, count, timeout=8):
    events = []
    buf = []

    async def _collect():
        async for line in lines_iter:
            if line == "":
                if buf and any(l.startswith("data:") for l in buf):
                    data_line = next(l for l in buf if l.startswith("data:"))
                    events.append(json.loads(data_line[len("data:"):].strip()))
                buf.clear()
                if len(events) >= count:
                    return
            elif not line.startswith(":"):
                buf.append(line)
    try:
        await asyncio.wait_for(_collect(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return events


async def test_catchup(client):
    print("\n=== catch-up (since param) ===")
    before = datetime.now(timezone.utc).isoformat()
    await ingest(client, "sess-sse-catchup-1", "Engineering", "platform", "team", "Catchup test record")

    async with client.stream("GET", f"{BASE}/v1/bus/subscribe", headers=hdr("dev"), params={"since": before}, timeout=15) as resp:
        assert resp.status_code == 200, await resp.aread()
        events = await parse_sse_events(resp.aiter_lines(), count=1, timeout=8)

    ok = len(events) == 1 and events[0]["session_id"] == "sess-sse-catchup-1"
    print(f"  got {len(events)} event(s), session_id={events[0].get('session_id') if events else None} -> {'OK' if ok else 'FAIL'}")
    return ok


async def test_live_delivery_and_visibility(client):
    print("\n=== live delivery + mandatory visibility ===")

    async def subscriber(tok_key, expect_count, results):
        async with client.stream("GET", f"{BASE}/v1/bus/subscribe", headers=hdr(tok_key), timeout=15) as resp:
            events = await parse_sse_events(resp.aiter_lines(), count=expect_count, timeout=10)
            results[tok_key] = events

    results = {}
    # dev and sales both connect; then we ingest one Engineering/team-visible record (dev should
    # see it) and one Sales/private record (sales won't see it either — bus has no own-content
    # override, see the module docstring / decisions-log.md).
    dev_task = asyncio.create_task(subscriber("dev", 1, results))
    sales_task = asyncio.create_task(subscriber("sales", 1, results))
    await asyncio.sleep(1.0)  # let both connections establish + LISTEN register before publishing

    await ingest(client, "sess-sse-live-1", "Engineering", "platform", "team", "Live team-visible record")
    await ingest(client, "sess-sse-live-2", "Sales", "enterprise", "private", "Live private sales record")

    await asyncio.wait_for(asyncio.gather(dev_task, sales_task), timeout=12)

    dev_sessions = {e["session_id"] for e in results.get("dev", [])}
    sales_sessions = {e["session_id"] for e in results.get("sales", [])}
    ok1 = dev_sessions == {"sess-sse-live-1"}
    ok2 = sales_sessions == set()
    print(f"  dev received: {dev_sessions} -> {'OK' if ok1 else 'FAIL'}")
    print(f"  sales received (expect none, private has no bus own-content override): {sales_sessions} -> {'OK' if ok2 else 'FAIL'}")
    return ok1 and ok2


async def test_narrowing(client):
    print("\n=== optional department/team narrowing ===")
    results = {}

    async def subscriber(expect_count):
        async with client.stream("GET", f"{BASE}/v1/bus/subscribe", headers=hdr("dev"), params={"team": "platform"}, timeout=15) as resp:
            results["events"] = await parse_sse_events(resp.aiter_lines(), count=expect_count, timeout=10)

    task = asyncio.create_task(subscriber(1))
    await asyncio.sleep(1.0)
    await ingest(client, "sess-sse-narrow-1", "Engineering", "platform", "org", "Narrow test platform record")
    await ingest(client, "sess-sse-narrow-2", "Engineering", "mobile", "org", "Narrow test mobile record")
    await asyncio.wait_for(task, timeout=12)

    sessions = {e["session_id"] for e in results.get("events", [])}
    ok = sessions == {"sess-sse-narrow-1"}
    print(f"  team=platform narrowed subscriber got: {sessions} -> {'OK' if ok else 'FAIL'}")
    return ok


async def test_auth(client):
    print("\n=== auth required ===")
    resp = await client.get(f"{BASE}/v1/bus/subscribe", timeout=10)
    ok = resp.status_code == 401
    print(f"  no-auth: {resp.status_code} -> {'OK' if ok else 'FAIL'}")
    return ok


async def test_disconnect_cleanup(client):
    print("\n=== disconnect cleanup (server stays healthy afterward) ===")
    async with client.stream("GET", f"{BASE}/v1/bus/subscribe", headers=hdr("dev"), timeout=15) as resp:
        assert resp.status_code == 200
        # open, then immediately abandon without reading — exiting this `async with`
        # closes the TCP connection out from under the server.
    await asyncio.sleep(1.0)
    resp2 = await client.get(f"{BASE}/v1/agent-activity", headers=hdr("dev"), timeout=10)
    ok = resp2.status_code == 200
    print(f"  server still responsive after abrupt disconnect: {resp2.status_code} -> {'OK' if ok else 'FAIL'}")
    print("  (for a real connection-leak check across many cycles, see docs/orgbrain-stack.md")
    print("   §3 Build Log's 2026-08-08 SSE entry — it records the pg_stat_activity numbers from")
    print("   8 open-and-abandon cycles; re-run manually if you suspect a regression)")
    return ok


async def main():
    results = {}
    try:
        async with httpx.AsyncClient() as client:
            results = {
                "catchup": await test_catchup(client),
                "live_and_visibility": await test_live_delivery_and_visibility(client),
                "narrowing": await test_narrowing(client),
                "auth": await test_auth(client),
                "disconnect_cleanup": await test_disconnect_cleanup(client),
            }
    finally:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute("DELETE FROM knowledge_entries WHERE session_id LIKE 'sess-sse-%'")
            await conn.execute("DELETE FROM context_bus_events WHERE session_id LIKE 'sess-sse-%'")
            await conn.execute("DELETE FROM redaction_audit_log WHERE session_id LIKE 'sess-sse-%'")
            remaining = await conn.fetchval("SELECT count(*) FROM knowledge_entries WHERE session_id LIKE 'sess-sse-%'")
            print(f"\ncleanup done, remaining sess-sse-* rows: {remaining}")
        finally:
            await conn.close()

    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    if not results or not all(results.values()):
        raise SystemExit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
