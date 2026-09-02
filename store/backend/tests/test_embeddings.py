"""Sanity check that fastembed + pgvector actually produce meaningful semantic
rankings, not just dimensionally-correct vectors. Talks to Postgres directly
(bypasses the REST API) — only needs Postgres up, not the REST server.

Written during the 2026-08-08 embeddings-provider reversal (see decisions-log.md) —
switching from a hosted API to a local model changed the vector dimension from
1536 to 384, and this is what proved the new model's output was actually usable
for ranking, not just the right shape.
"""

import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from fastembed import TextEmbedding

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

TEXTS = {
    "t-embed-cat": "The cat sat on the mat and fell asleep in the sun.",
    "t-embed-feline": "A sleepy feline rested on the rug near the window.",
    "t-embed-finance": "Quarterly revenue increased by twelve percent year over year.",
}
QUERY = "a cat napping on a rug"


async def main():
    model = TextEmbedding()
    vectors = {k: list(model.embed([v]))[0] for k, v in TEXTS.items()}
    query_vec = list(model.embed([QUERY]))[0]
    print(f"embedding dim: {len(query_vec)}")
    assert len(query_vec) == 384, f"expected 384 dims, got {len(query_vec)}"

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        for key, text in TEXTS.items():
            record_id = await conn.fetchval(
                """
                INSERT INTO knowledge_entries
                    (session_id, source_system, department, author_user_id, visibility,
                     consent_basis, consent_actor_type, consent_actor_id, status, title, summary, outcome)
                VALUES ($1, 'test-harness', 'Engineering', 'u1', 'team', 'user_opted_in', 'user', 'u1',
                        'completed', $2, $2, 'insight_found')
                RETURNING record_id
                """,
                key, text,
            )
            await conn.execute(
                "INSERT INTO knowledge_embeddings (record_id, embedding) VALUES ($1, $2)",
                record_id, str(vectors[key].tolist()),
            )

        rows = await conn.fetch(
            """
            SELECT ke.session_id, ke.title, kem.embedding <=> $1 AS distance
            FROM knowledge_embeddings kem JOIN knowledge_entries ke ON ke.record_id = kem.record_id
            WHERE ke.session_id LIKE 't-embed-%'
            ORDER BY distance
            """,
            str(query_vec.tolist()),
        )
        print(f"\nQuery: {QUERY!r}")
        for r in rows:
            print(f"  {r['session_id']:16s} distance={r['distance']:.4f}  {r['title'][:50]}")

        nearest = rows[0]["session_id"]
        ok = nearest in ("t-embed-cat", "t-embed-feline") and rows[-1]["session_id"] == "t-embed-finance"
        print("\nPASS" if ok else "\nFAIL")
        if not ok:
            raise SystemExit(1)
    finally:
        await conn.execute("DELETE FROM knowledge_entries WHERE session_id LIKE 't-embed-%'")
        remaining = await conn.fetchval("SELECT count(*) FROM knowledge_entries WHERE session_id LIKE 't-embed-%'")
        print(f"cleanup done, remaining: {remaining}")
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
