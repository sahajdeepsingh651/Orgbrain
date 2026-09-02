"""Applies db/schema.sql to the database at DATABASE_URL. Safe to re-run."""

import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    schema_sql = SCHEMA_PATH.read_text()

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(schema_sql)
    finally:
        await conn.close()

    print(f"Applied {SCHEMA_PATH} to {database_url.split('@')[-1]}")


if __name__ == "__main__":
    asyncio.run(main())
