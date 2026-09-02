"""G4 end-to-end against the LIVE Context Bus (not a fake).

Seeds two records under different identities, then drives
gateway.flows.handle_read through the real bus_client and asserts:

  1. an authorised record is retrieved and injected
  2. a secret sitting in the bus is REDACTED before injection
     (the store does no PII scanning by design, so the bus can genuinely
      hold one — this is not a hypothetical)
  3. visibility is enforced: a Sales identity cannot see a platform-team record
  4. an unknown account never reaches the bus at all
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "store" / ".env")

from gateway import bus_client, flows                            # noqa: E402
from gateway.protocol.anthropic_adapter import AnthropicAdapter  # noqa: E402

ADAPTER = AnthropicAdapter()
BASE = "http://127.0.0.1:8000"
SECRET = "sk-test-livebus9999"
PREFIX = f"sess-e2e-{uuid.uuid4().hex[:6]}"

# from store/config/account_map.json
ENG_ACCOUNT = "aaaaaaaa-0000-4000-8000-000000000001"      # u-dev, Engineering/platform
SALES_ACCOUNT = "cccccccc-0000-4000-8000-000000000003"    # u-sales-1, Sales/enterprise
UNKNOWN_ACCOUNT = "00000000-0000-0000-0000-00000000dead"


def body(text, account, session="s1"):
    import json as _json
    meta = {"user_id": _json.dumps(
        {"device_id": "d" * 8, "account_uuid": account, "session_id": session})}
    return {
        "model": "claude-sonnet-5", "max_tokens": 1024, "stream": True,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
        "metadata": meta,
    }


def injected(nr):
    return "\n".join(b.get("text", "") for m in nr.messages if m.role == "system"
                     for b in m.content if b.get("type") == "text")


async def seed(client, session_id, title, summary, token, visibility="team"):
    payload = {
        "source_system": "e2e-harness",
        "captured_by": {"user_id": "ignored-by-s5", "agent_id": "agent-e2e"},
        "session_id": session_id, "content": f"{title}. {summary}",
        "sensitivity_flags": {"contains_pii": False, "contains_credentials": False,
                              "redaction_applied": False, "redaction_count": 0},
        "visibility": visibility, "status": "completed",
        "knowledge": {"title": title, "summary": summary, "outcome": "decision_made"},
        "hint": {"department": "Engineering", "team": "platform"},
    }
    r = await client.post(f"{BASE}/v1/ingest",
                          headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=30)
    assert r.status_code == 201, (r.status_code, r.text[:300])
    return r.json()["record_id"]


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    results = {}
    try:
        async with httpx.AsyncClient() as c:
            await seed(c, f"{PREFIX}-a",
                       "Kafka consumer rebalance tuning",
                       f"Set session.timeout.ms to 45000. Ops key {SECRET} is in the runbook.",
                       "dev-local-token", visibility="team")
            await seed(c, f"{PREFIX}-b",
                       "Kafka partition sizing",
                       "Chose 24 partitions per topic after load testing.",
                       "dev-local-token", visibility="team")
        # embeddings are written synchronously at ingest, so it's queryable now

        # 1 + 2 — authorised retrieval, and redaction of a secret held in the bus
        vault = {}
        nr, diag = await flows.handle_read(
            ADAPTER.to_normalized(body("ESDS_SEARCH kafka consumer rebalance", ENG_ACCOUNT)),
            vault)
        ctx = injected(nr)
        ok_retrieve = diag["injected"] and diag["hits"] >= 1
        ok_redact = (SECRET not in ctx) and any(t.startswith("⟦SECRET_") for t in vault)
        print(f"1. retrieved   : hits={diag['hits']} injected={diag['injected']} -> "
              f"{'PASS' if ok_retrieve else 'FAIL'}")
        print(f"2. redacted    : secret_in_context={SECRET in ctx} vault={list(vault)} -> "
              f"{'PASS' if ok_redact else 'FAIL'}")
        if ok_retrieve:
            print("   injected context (first 3 lines):")
            for line in ctx.splitlines()[:3]:
                print(f"     | {line}")
        results["retrieve"] = ok_retrieve
        results["redact"] = ok_redact

        # 3 — visibility enforced by the bus, for a different department
        nr2, diag2 = await flows.handle_read(
            ADAPTER.to_normalized(body("ESDS_SEARCH kafka consumer rebalance", SALES_ACCOUNT)),
            {})
        ok_vis = diag2["hits"] == 0 and not diag2["injected"]
        print(f"3. visibility  : sales hits={diag2['hits']} (expect 0) -> "
              f"{'PASS' if ok_vis else 'FAIL'}")
        results["visibility"] = ok_vis

        # 4 — unknown account never reaches the bus
        _, diag3 = await flows.handle_read(
            ADAPTER.to_normalized(body("ESDS_SEARCH anything", UNKNOWN_ACCOUNT)), {})
        ok_closed = diag3["reason"] == "unknown_account" and not diag3["injected"]
        print(f"4. fail-closed : reason={diag3['reason']!r} -> {'PASS' if ok_closed else 'FAIL'}")
        results["fail_closed"] = ok_closed

    finally:
        await bus_client.aclose()
        for t in ("knowledge_entries", "redaction_audit_log", "context_bus_events"):
            await conn.execute(f"DELETE FROM {t} WHERE session_id LIKE '{PREFIX}%'")
        await conn.close()

    print()
    print("RESULT:", "ALL PASS" if all(results.values()) else "FAILURES: " +
          ", ".join(k for k, v in results.items() if not v))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
