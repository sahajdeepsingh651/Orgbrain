"""Integration tests for GET /v1/search, /v1/agent-activity, /v1/handoff (build step 4).

Prerequisites: Postgres up and the REST server running (see docs/orgbrain-setup.md).
Uses the 3 dev identities from docs/orgbrain-setup.md §4 (dev/eng2/sales) to
exercise the full visibility matrix (private/team/department/org) across 5 records.
"""

import asyncio
import os
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


RECORDS = [
    dict(session_id="sess-vis-A", user_id="u-dev", agent_id="agent-A", dept="Engineering", team="platform",
         visibility="private", title="Private platform record about auth tokens", summary="private eng content"),
    dict(session_id="sess-vis-B", user_id="u-eng-2", agent_id="agent-B", dept="Engineering", team="mobile",
         visibility="team", title="Team-visible mobile record about push notifications", summary="team mobile content"),
    dict(session_id="sess-vis-C", user_id="u-dev", agent_id=None, dept="Engineering", team="platform",
         visibility="department", title="Department-visible platform record about deployment", summary="dept eng content"),
    dict(session_id="sess-vis-D", user_id="u-sales-1", agent_id="agent-D", dept="Sales", team="enterprise",
         visibility="org", title="Org-visible sales record about enterprise deal", summary="org sales content"),
    dict(session_id="sess-vis-E", user_id="u-sales-1", agent_id=None, dept="Sales", team="enterprise",
         visibility="private", title="Private sales record about pricing negotiation", summary="private sales content"),
]

# expected visibility: {caller_key: set of session_ids that SHOULD be visible}
EXPECTED = {
    "dev":   {"sess-vis-A", "sess-vis-C", "sess-vis-D"},
    "eng2":  {"sess-vis-B", "sess-vis-C", "sess-vis-D"},
    "sales": {"sess-vis-D", "sess-vis-E"},
}


async def ingest_all(client):
    """S5 — author identity now comes from the bearer token, not the body.
    Seed each record under its owner's token so the visibility matrix the
    EXPECTED table asserts stays correct. Before S5, all 5 records were
    seeded under hdr("dev") with the body declaring user_id="u-dev"/"u-eng-2"
    /etc. — which worked only because the store trusted the body. After S5,
    the store IGNORES body user_id and derives from the token, so the old
    seeding would put all 5 records under u-dev and the matrix would break."""
    owner_token = {
        "sess-vis-A": "dev", "sess-vis-B": "eng2", "sess-vis-C": "dev",
        "sess-vis-D": "sales", "sess-vis-E": "sales",
    }
    ids = {}
    for r in RECORDS:
        payload = {
            "source_system": "test-harness",
            "captured_by": {"user_id": r["user_id"], **({"agent_id": r["agent_id"]} if r["agent_id"] else {})},
            "session_id": r["session_id"],
            "content": r["summary"],
            "sensitivity_flags": {"contains_pii": False, "contains_credentials": False, "redaction_applied": False, "redaction_count": 0},
            "visibility": r["visibility"],
            "status": "completed",
            "knowledge": {"title": r["title"], "summary": r["summary"], "outcome": "insight_found"},
            "hint": {"department": r["dept"], "team": r["team"]},
        }
        tok_key = owner_token[r["session_id"]]
        resp = await client.post(f"{BASE}/v1/ingest", headers=hdr(tok_key), json=payload, timeout=15)
        assert resp.status_code == 201, f"ingest failed for {r['session_id']}: {resp.status_code} {resp.text}"
        ids[r["session_id"]] = resp.json()["record_id"]
    return ids


async def test_search_visibility(client):
    print("\n=== /v1/search visibility matrix ===")
    all_ok = True
    for caller, expected_visible in EXPECTED.items():
        resp = await client.get(f"{BASE}/v1/search", headers=hdr(caller), params={"q": "record", "limit": 50}, timeout=15)
        assert resp.status_code == 200, resp.text
        got_sessions = {r["session_id"] for r in resp.json()["results"] if r["session_id"].startswith("sess-vis-")}
        ok = got_sessions == expected_visible
        all_ok = all_ok and ok
        print(f"  {caller:6s}: got={sorted(got_sessions)} expected={sorted(expected_visible)} -> {'OK' if ok else 'FAIL'}")
    return all_ok


async def test_search_department_team_narrowing(client):
    print("\n=== /v1/search department/team narrowing ===")
    # dev can see A, C, D. Narrow to department=Sales -> should only get D (the one Sales record dev can see)
    resp = await client.get(f"{BASE}/v1/search", headers=hdr("dev"), params={"q": "record", "limit": 50, "department": "Sales"}, timeout=15)
    got = {r["session_id"] for r in resp.json()["results"] if r["session_id"].startswith("sess-vis-")}
    ok1 = got == {"sess-vis-D"}
    print(f"  dev + department=Sales narrowing: got={sorted(got)} -> {'OK' if ok1 else 'FAIL'}")

    resp2 = await client.get(f"{BASE}/v1/search", headers=hdr("dev"), params={"q": "record", "limit": 50, "team": "platform"}, timeout=15)
    got2 = {r["session_id"] for r in resp2.json()["results"] if r["session_id"].startswith("sess-vis-")}
    ok2 = got2 == {"sess-vis-A", "sess-vis-C"}
    print(f"  dev + team=platform narrowing: got={sorted(got2)} -> {'OK' if ok2 else 'FAIL'}")
    return ok1 and ok2


async def test_search_semantic_relevance(client):
    print("\n=== /v1/search semantic + keyword relevance ===")
    # "push notifications" should surface the mobile record (sess-vis-B) even though eng2 also
    # has visibility into C and D — check it's ranked #1 (closest) for eng2.
    resp = await client.get(f"{BASE}/v1/search", headers=hdr("eng2"), params={"q": "push notifications", "limit": 50}, timeout=15)
    results = [r for r in resp.json()["results"] if r["session_id"].startswith("sess-vis-")]
    ok = len(results) > 0 and results[0]["session_id"] == "sess-vis-B"
    print(f"  top result for 'push notifications': {results[0]['session_id'] if results else None} -> {'OK' if ok else 'FAIL'}")
    return ok


async def test_agent_activity(client):
    print("\n=== /v1/agent-activity visibility ===")
    # dev should see agent-A (own, private) and agent-D (org), NOT agent-B (mobile team, not visible to dev)
    resp = await client.get(f"{BASE}/v1/agent-activity", headers=hdr("dev"), timeout=15)
    agent_ids = {r["agent_id"] for r in resp.json()["results"]}
    ok1 = agent_ids >= {"agent-A", "agent-D"} and "agent-B" not in agent_ids
    print(f"  dev sees agent_ids={sorted(agent_ids)} -> {'OK' if ok1 else 'FAIL'}")

    # eng2 should see agent-B (own team) and agent-D (org), NOT agent-A (private, not own)
    resp2 = await client.get(f"{BASE}/v1/agent-activity", headers=hdr("eng2"), timeout=15)
    agent_ids2 = {r["agent_id"] for r in resp2.json()["results"]}
    ok2 = agent_ids2 >= {"agent-D"} and "agent-A" not in agent_ids2
    print(f"  eng2 sees agent_ids={sorted(agent_ids2)} -> {'OK' if ok2 else 'FAIL'}")

    # team filter: dev filtered to team=platform should see only agent-A
    resp3 = await client.get(f"{BASE}/v1/agent-activity", headers=hdr("dev"), params={"team": "platform"}, timeout=15)
    agent_ids3 = {r["agent_id"] for r in resp3.json()["results"]}
    ok3 = agent_ids3 == {"agent-A"}
    print(f"  dev + team=platform filter: agent_ids={sorted(agent_ids3)} -> {'OK' if ok3 else 'FAIL'}")

    return ok1 and ok2 and ok3


async def test_handoff(client):
    print("\n=== /v1/handoff/{session_id} ===")
    r1 = await client.get(f"{BASE}/v1/handoff/sess-vis-A", headers=hdr("dev"), timeout=15)
    ok1 = r1.status_code == 200 and r1.json()["title"] == RECORDS[0]["title"]
    print(f"  dev -> sess-vis-A (own private): {r1.status_code} -> {'OK' if ok1 else 'FAIL'}")

    r2 = await client.get(f"{BASE}/v1/handoff/sess-vis-A", headers=hdr("sales"), timeout=15)
    ok2 = r2.status_code == 404
    print(f"  sales -> sess-vis-A (should be 404): {r2.status_code} -> {'OK' if ok2 else 'FAIL'}")

    r3 = await client.get(f"{BASE}/v1/handoff/sess-vis-D", headers=hdr("dev"), timeout=15)
    ok3 = r3.status_code == 200
    print(f"  dev -> sess-vis-D (org): {r3.status_code} -> {'OK' if ok3 else 'FAIL'}")

    r4 = await client.get(f"{BASE}/v1/handoff/sess-does-not-exist", headers=hdr("dev"), timeout=15)
    ok4 = r4.status_code == 404
    print(f"  dev -> nonexistent session: {r4.status_code} -> {'OK' if ok4 else 'FAIL'}")

    return ok1 and ok2 and ok3 and ok4


async def test_auth_failures(client):
    print("\n=== auth failures on serving endpoints ===")
    r1 = await client.get(f"{BASE}/v1/search", params={"q": "x"}, timeout=15)
    r2 = await client.get(f"{BASE}/v1/agent-activity", timeout=15)
    r3 = await client.get(f"{BASE}/v1/handoff/sess-vis-A", timeout=15)
    ok = r1.status_code == 401 and r2.status_code == 401 and r3.status_code == 401
    print(f"  no-auth requests: search={r1.status_code} agent-activity={r2.status_code} handoff={r3.status_code} -> {'OK' if ok else 'FAIL'}")
    return ok


async def main():
    results = {}
    try:
        async with httpx.AsyncClient() as client:
            await ingest_all(client)
            results = {
                "search_visibility": await test_search_visibility(client),
                "search_narrowing": await test_search_department_team_narrowing(client),
                "search_semantic": await test_search_semantic_relevance(client),
                "agent_activity": await test_agent_activity(client),
                "handoff": await test_handoff(client),
                "auth_failures": await test_auth_failures(client),
            }
    finally:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute("DELETE FROM knowledge_entries WHERE session_id LIKE 'sess-vis-%'")
            await conn.execute("DELETE FROM redaction_audit_log WHERE session_id LIKE 'sess-vis-%'")
            await conn.execute("DELETE FROM context_bus_events WHERE session_id LIKE 'sess-vis-%'")
            remaining = await conn.fetchval("SELECT count(*) FROM knowledge_entries WHERE session_id LIKE 'sess-vis-%'")
            print(f"\ncleanup done, remaining sess-vis-* rows: {remaining}")
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
