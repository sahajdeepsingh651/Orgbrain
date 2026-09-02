"""Integration tests for POST /v1/ingest (build step 2).

Prerequisites: Postgres up (docker compose up -d) and the REST server running
(uvicorn app.main:app --port 8000) — see docs/data-passport-setup.md.

Covers: happy path (with and without domain_data), auth failure before any Bronze
write, and every validation-failure branch (each must return 422 + a quarantined
redaction_audit_log row + zero knowledge_entries rows). Originally written during
build step 2 — see docs/data-passport-stack.md §3 Build Log (2026-08-08 entries)
for what these caught the first time around (a real SQL column/value-count bug).
"""

import asyncio
import os
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

BASE_URL = "http://127.0.0.1:8000/v1/ingest"
TOKEN = "dev-local-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def base_payload(session_id: str) -> dict:
    return {
        "source_system": "claude-code",
        "captured_by": {"user_id": "u-test", "agent_id": "agent-test"},
        "session_id": session_id,
        "content": "some already-redacted content",
        "sensitivity_flags": {
            "contains_pii": False, "contains_credentials": False,
            "redaction_applied": False, "redaction_count": 0,
        },
        "visibility": "team",
        "status": "completed",
        "knowledge": {"title": "t", "summary": "s", "outcome": "insight_found"},
        "hint": {"department": "Engineering", "team": "platform"},
    }


def delpath(d: dict, path: str) -> dict:
    node = d
    parts = path.split(".")
    for p in parts[:-1]:
        node = node[p]
    del node[parts[-1]]
    return d


NEGATIVE_CASES = []


def case(name, mutate):
    session_id = f"sess-ingest-neg-{name}"
    p = base_payload(session_id)
    mutate(p)
    NEGATIVE_CASES.append((name, session_id, p))


case("missing-session-id", lambda p: delpath(p, "session_id"))
case("missing-content", lambda p: delpath(p, "content"))
case("content-not-string", lambda p: p.update(content=12345))
# S5 — captured_by.user_id and hint.department are no longer validated server-side:
# author/dept/team come from the authenticated token, NOT the body. Removing
# these as negative cases was explicitly flagged by the plan when S5 lands.
case("bad-visibility", lambda p: p.update(visibility="public"))
case("bad-status", lambda p: p.update(status="cancelled"))
case("missing-title", lambda p: delpath(p, "knowledge.title"))
case("missing-summary", lambda p: delpath(p, "knowledge.summary"))
case("bad-outcome", lambda p: p["knowledge"].update(outcome="wontfix"))
case("domain-without-domain_data", lambda p: p.update(domain="engineering.v1"))
case("domain_data-without-domain", lambda p: p.update(domain_data={"repo": "x"}))
case("unknown-domain", lambda p: p.update(domain="marketing.v1", domain_data={"x": "y"}))
case("domain_data-bad-url", lambda p: p.update(
    domain="engineering.v1",
    domain_data={"repo": "org/x", "pr_link": "not-a-url", "root_cause": "r", "fix_type": "bugfix"},
))
case("domain_data-bad-enum", lambda p: p.update(
    domain="engineering.v1",
    domain_data={"repo": "org/x", "pr_link": "https://x/1", "root_cause": "r", "fix_type": "typo-fix"},
))
case("domain_data-undeclared-field", lambda p: p.update(
    domain="engineering.v1",
    domain_data={"repo": "org/x", "pr_link": "https://x/1", "root_cause": "r", "fix_type": "bugfix", "extra_field": "nope"},
))
case("domain_data-wrong-array-type", lambda p: p.update(
    domain="engineering.v1",
    domain_data={"repo": "org/x", "files_changed": "not-an-array", "root_cause": "r", "fix_type": "bugfix"},
))


async def test_happy_path(client, conn) -> bool:
    print("\n=== happy path ===")
    payload_full = {
        **base_payload("sess-ingest-happy-1"),
        "knowledge": {
            "title": "Fixed token refresh race condition", "summary": "s", "outcome": "issue_resolved",
            "key_points": ["point one"], "next_steps": ["step one"],
        },
        "domain": "engineering.v1",
        "domain_data": {"repo": "org/x", "files_changed": ["a.ts"], "pr_link": "https://x/1", "root_cause": "r", "fix_type": "bugfix"},
    }
    resp = await client.post(BASE_URL, headers=HEADERS, json=payload_full, timeout=15)
    ok1 = resp.status_code == 201 and "record_id" in resp.json() and resp.json()["status"] == "committed"
    print(f"  full record with domain_data: {resp.status_code} -> {'OK' if ok1 else 'FAIL'}")

    payload_min = base_payload("sess-ingest-happy-2")
    resp2 = await client.post(BASE_URL, headers=HEADERS, json=payload_min, timeout=15)
    ok2 = resp2.status_code == 201
    print(f"  minimal record, no domain: {resp2.status_code} -> {'OK' if ok2 else 'FAIL'}")

    row = await conn.fetchrow(
        "SELECT vector_dims(embedding) AS dims FROM knowledge_embeddings ke "
        "JOIN knowledge_entries e ON e.record_id = ke.record_id WHERE e.session_id = 'sess-ingest-happy-1'"
    )
    ok3 = row is not None and row["dims"] == 384
    print(f"  embedding computed with correct dims: {row['dims'] if row else None} -> {'OK' if ok3 else 'FAIL'}")
    return ok1 and ok2 and ok3


async def test_auth(client) -> bool:
    print("\n=== auth ===")
    r1 = await client.post(BASE_URL, headers={"Content-Type": "application/json"}, json={}, timeout=10)
    r2 = await client.post(BASE_URL, headers={"Authorization": "Bearer wrong-token", "Content-Type": "application/json"}, json={}, timeout=10)
    ok = r1.status_code == 401 and r2.status_code == 401
    print(f"  no token: {r1.status_code}, wrong token: {r2.status_code} -> {'OK' if ok else 'FAIL'}")
    return ok


async def test_negative_cases(client, conn) -> bool:
    print("\n=== validation-failure cases ===")
    all_ok = True
    for name, session_id, payload in NEGATIVE_CASES:
        resp = await client.post(BASE_URL, headers=HEADERS, json=payload, timeout=15)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        ok_status = resp.status_code == 422
        quarantine_id = body.get("quarantine_id")
        audit_row = None
        if quarantine_id:
            audit_row = await conn.fetchrow(
                "SELECT outcome FROM redaction_audit_log WHERE quarantine_id = $1", quarantine_id
            )
        audit_ok = audit_row is not None and audit_row["outcome"] == "quarantined"
        entry_count = await conn.fetchval(
            "SELECT count(*) FROM knowledge_entries WHERE session_id = $1", session_id
        )
        no_entry_ok = entry_count == 0
        row_ok = ok_status and audit_ok and no_entry_ok
        all_ok = all_ok and row_ok
        marker = "OK " if row_ok else "FAIL"
        print(f"  {name:35s} {marker} field={body.get('field')} reason={body.get('error')}")
    return all_ok


async def main():
    async with httpx.AsyncClient() as client:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            results = {
                "happy_path": await test_happy_path(client, conn),
                "auth": await test_auth(client),
                "negative_cases": await test_negative_cases(client, conn),
            }
        finally:
            await conn.execute("DELETE FROM knowledge_entries WHERE session_id LIKE 'sess-ingest-%'")
            await conn.execute("DELETE FROM redaction_audit_log WHERE session_id LIKE 'sess-ingest-%' OR session_id IS NULL")
            await conn.execute("DELETE FROM context_bus_events WHERE session_id LIKE 'sess-ingest-%'")
            remaining = await conn.fetchval("SELECT count(*) FROM knowledge_entries")
            print(f"\ncleanup done, remaining knowledge_entries: {remaining}")
            await conn.close()

    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    if not all(results.values()):
        raise SystemExit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
