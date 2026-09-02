"""V2 — concurrent Idempotency-Key handling on POST /v1/ingest.

The pre-check in `ingest` (SELECT ... WHERE idempotency_key = $1) is a
TOCTOU read: N concurrent requests carrying the same key all miss it, all
reach the INSERT, and every loser violates
`knowledge_entries_idempotency_key_idx`. Before V2 the losers got a 500;
the DB constraint still held (one row), so this is a caller-facing defect,
not a data-integrity one — which is exactly why it survived the sequential
retry test that `test_ingest.py` runs.

Correct behaviour: exactly one 201, N-1 × 200 "deduplicated", every
response naming the SAME record_id, and exactly one row.

Prerequisites (same as the other integration tests): Postgres up and the
REST server running. See docs/orgbrain-setup.md.
Run:  .venv/bin/python tests/test_idempotency_race.py
"""

import asyncio
import os
import uuid
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

BASE = "http://127.0.0.1:8000"
TOKEN = "dev-local-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

CONCURRENCY = 5
SESSION_PREFIX = "sess-race-"


def payload(session_id: str) -> dict:
    return {
        "source_system": "test-harness",
        "captured_by": {"user_id": "u-dev", "agent_id": "agent-race"},
        "session_id": session_id,
        "content": "Concurrent idempotency probe.",
        "sensitivity_flags": {
            "contains_pii": False, "contains_credentials": False,
            "redaction_applied": False, "redaction_count": 0,
        },
        "visibility": "team",
        "status": "completed",
        "knowledge": {"title": "Race probe", "summary": "Race probe", "outcome": "insight_found"},
        "hint": {"department": "Engineering", "team": "platform"},
    }


async def _post(client, body, key):
    r = await client.post(
        f"{BASE}/v1/ingest",
        headers={**HEADERS, "Idempotency-Key": key},
        json=body,
        timeout=30,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text[:200]}


async def test_concurrent_same_key(client, conn) -> bool:
    """N identical requests fired at once: one creates, the rest dedup."""
    session_id = f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"
    key = f"idem-{uuid.uuid4().hex[:12]}"
    body = payload(session_id)

    results = await asyncio.gather(
        *[_post(client, body, key) for _ in range(CONCURRENCY)],
        return_exceptions=True,
    )

    codes, ids = [], set()
    for res in results:
        if isinstance(res, Exception):
            codes.append(f"EXC:{type(res).__name__}")
            continue
        codes.append(res[0])
        if isinstance(res[1], dict) and res[1].get("record_id"):
            ids.add(res[1]["record_id"])

    rows = await conn.fetchval(
        "SELECT count(*) FROM knowledge_entries WHERE session_id = $1", session_id
    )

    n201 = sum(1 for c in codes if c == 201)
    n200 = sum(1 for c in codes if c == 200)
    n5xx = sum(1 for c in codes if isinstance(c, int) and c >= 500)

    ok = (
        n201 == 1
        and n200 == CONCURRENCY - 1
        and n5xx == 0
        and rows == 1
        and len(ids) == 1          # every caller learns the SAME record_id
    )
    print(f"  concurrent-same-key: codes={codes} rows={rows} distinct_ids={len(ids)} -> {'PASS' if ok else 'FAIL'}")
    if n5xx:
        print("    -> 5xx present: UniqueViolation on the idempotency index is unhandled.")
    return ok


async def test_sequential_same_key(client, conn) -> bool:
    """The already-working path — kept here so a regression in the fast
    path is caught by the same file as the race."""
    session_id = f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"
    key = f"idem-{uuid.uuid4().hex[:12]}"
    body = payload(session_id)

    first = await _post(client, body, key)
    second = await _post(client, body, key)
    rows = await conn.fetchval(
        "SELECT count(*) FROM knowledge_entries WHERE session_id = $1", session_id
    )
    ok = (
        first[0] == 201
        and second[0] == 200
        and second[1].get("status") == "deduplicated"
        and first[1].get("record_id") == second[1].get("record_id")
        and rows == 1
    )
    print(f"  sequential-same-key: {first[0]} then {second[0]} rows={rows} -> {'PASS' if ok else 'FAIL'}")
    return ok


async def test_distinct_keys_are_not_deduped(client, conn) -> bool:
    """Guard against over-deduping: different keys must create different rows."""
    session_id = f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"
    body = payload(session_id)
    a = await _post(client, body, f"idem-{uuid.uuid4().hex[:12]}")
    b = await _post(client, body, f"idem-{uuid.uuid4().hex[:12]}")
    rows = await conn.fetchval(
        "SELECT count(*) FROM knowledge_entries WHERE session_id = $1", session_id
    )
    ok = a[0] == 201 and b[0] == 201 and rows == 2
    print(f"  distinct-keys: {a[0]},{b[0]} rows={rows} (expect 2) -> {'PASS' if ok else 'FAIL'}")
    return ok


async def test_no_key_still_works(client, conn) -> bool:
    """No Idempotency-Key header at all: unchanged 201-per-request behaviour."""
    session_id = f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"
    body = payload(session_id)
    async with httpx.AsyncClient() as plain:
        r1 = await plain.post(f"{BASE}/v1/ingest", headers=HEADERS, json=body, timeout=30)
        r2 = await plain.post(f"{BASE}/v1/ingest", headers=HEADERS, json=body, timeout=30)
    rows = await conn.fetchval(
        "SELECT count(*) FROM knowledge_entries WHERE session_id = $1", session_id
    )
    ok = r1.status_code == 201 and r2.status_code == 201 and rows == 2
    print(f"  no-key: {r1.status_code},{r2.status_code} rows={rows} (expect 2) -> {'PASS' if ok else 'FAIL'}")
    return ok


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    results = {}
    try:
        async with httpx.AsyncClient() as client:
            print("V2 — ingest idempotency under concurrency")
            results["concurrent_same_key"] = await test_concurrent_same_key(client, conn)
            results["sequential_same_key"] = await test_sequential_same_key(client, conn)
            results["distinct_keys"] = await test_distinct_keys_are_not_deduped(client, conn)
            results["no_key"] = await test_no_key_still_works(client, conn)
    finally:
        # Scoped to this file's own prefix only — deliberately NOT the
        # `OR session_id IS NULL` clause test_ingest.py uses, which wipes
        # every NULL-session audit row including other components'.
        await conn.execute(
            f"DELETE FROM knowledge_entries WHERE session_id LIKE '{SESSION_PREFIX}%'")
        await conn.execute(
            f"DELETE FROM redaction_audit_log WHERE session_id LIKE '{SESSION_PREFIX}%'")
        await conn.execute(
            f"DELETE FROM context_bus_events WHERE session_id LIKE '{SESSION_PREFIX}%'")
        await conn.close()

    print("\n=== SUMMARY ===")
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    if not results or not all(results.values()):
        raise SystemExit(1)
    print("all passed")


if __name__ == "__main__":
    asyncio.run(main())
