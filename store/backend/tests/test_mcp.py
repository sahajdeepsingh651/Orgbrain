"""Integration test for the MCP tool wrappers (build step 6).

Prerequisites: Postgres up and the REST server running (used to seed test data via
POST /v1/ingest — see docs/orgbrain-setup.md). This test spawns
backend/mcp_server.py itself as a subprocess; don't run it separately first.

Uses a real MCP client (mcp.client.stdio + ClientSession) talking to an actual
subprocess over stdio — not calling the Python functions directly — so this also
exercises the real MCP protocol round-trip (tool registration, structured content
unwrapping, tool-level error reporting).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = REPO_ROOT / "backend"
load_dotenv(REPO_ROOT / ".env")

BASE = "http://127.0.0.1:8000"
INGEST_HEADERS = {"Authorization": "Bearer dev-local-token", "Content-Type": "application/json"}


async def ingest(client, session_id, dept, team, visibility, title, agent_id=None, user_id="u-dev"):
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
    resp = await client.post(f"{BASE}/v1/ingest", headers=INGEST_HEADERS, json=payload, timeout=15)
    assert resp.status_code == 201, resp.text
    return resp.json()["record_id"]


def result_to_obj(call_result):
    """MCP tool results carry structured content; unwrap it to a plain Python object.
    Bare list-returning tools get auto-wrapped by the SDK as {"result": [...]}."""
    if call_result.structured_content is not None:
        sc = call_result.structured_content
        return sc["result"] if isinstance(sc, dict) and set(sc.keys()) == {"result"} else sc
    text = call_result.content[0].text
    return json.loads(text)


async def main():
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(BACKEND / "mcp_server.py")],
        cwd=str(BACKEND),
        env={**os.environ, "MCP_API_TOKEN": "dev-local-token"},
    )

    results = {}
    try:
        async with httpx.AsyncClient() as http_client:
            await ingest(http_client, "sess-mcp-1", "Engineering", "platform", "org",
                         "MCP test record about push notifications", agent_id="agent-mcp-1")
            await ingest(http_client, "sess-mcp-2", "Sales", "enterprise", "private",
                         "MCP private sales record", user_id="u-sales-1")  # dev should NOT see this

            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    tool_names = {t.name for t in tools.tools}
                    results["tools_registered"] = tool_names == {"search_knowledge", "get_agent_activity", "handoff"}
                    print(f"1. registered tools: {sorted(tool_names)} -> {'OK' if results['tools_registered'] else 'FAIL'}")

                    search_result = await session.call_tool("search_knowledge", {"query": "push notifications", "limit": 10})
                    search_obj = result_to_obj(search_result)
                    search_sessions = {r["session_id"] for r in search_obj} if isinstance(search_obj, list) else set()
                    ok2 = "sess-mcp-1" in search_sessions and "sess-mcp-2" not in search_sessions
                    results["search_visibility"] = ok2
                    print(f"2. search_knowledge: got sessions {search_sessions} -> {'OK' if ok2 else 'FAIL'}")

                    activity_result = await session.call_tool("get_agent_activity", {})
                    activity_obj = result_to_obj(activity_result)
                    agent_ids = {r["agent_id"] for r in activity_obj} if isinstance(activity_obj, list) else set()
                    ok3 = "agent-mcp-1" in agent_ids
                    results["agent_activity"] = ok3
                    print(f"3. get_agent_activity: agent_ids {agent_ids} -> {'OK' if ok3 else 'FAIL'}")

                    handoff_result = await session.call_tool("handoff", {"session_id": "sess-mcp-1"})
                    handoff_obj = result_to_obj(handoff_result)
                    ok4 = handoff_obj.get("session_id") == "sess-mcp-1"
                    results["handoff_found"] = ok4
                    print(f"4. handoff(sess-mcp-1): {handoff_obj.get('title')} -> {'OK' if ok4 else 'FAIL'}")

                    handoff_bad = await session.call_tool("handoff", {"session_id": "sess-does-not-exist"})
                    ok5 = bool(handoff_bad.is_error)
                    results["handoff_not_found_is_error"] = ok5
                    print(f"5. handoff(nonexistent): is_error={handoff_bad.is_error} -> {'OK' if ok5 else 'FAIL'}")

                    handoff_private = await session.call_tool("handoff", {"session_id": "sess-mcp-2"})
                    ok6 = bool(handoff_private.is_error)
                    results["handoff_private_denied"] = ok6
                    print(f"6. handoff(private, not mine): is_error={handoff_private.is_error} -> {'OK' if ok6 else 'FAIL'}")
    finally:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute("DELETE FROM knowledge_entries WHERE session_id LIKE 'sess-mcp-%'")
            await conn.execute("DELETE FROM context_bus_events WHERE session_id LIKE 'sess-mcp-%'")
            await conn.execute("DELETE FROM redaction_audit_log WHERE session_id LIKE 'sess-mcp-%'")
            remaining = await conn.fetchval("SELECT count(*) FROM knowledge_entries WHERE session_id LIKE 'sess-mcp-%'")
            print(f"\ncleanup done, remaining sess-mcp-* rows: {remaining}")
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
