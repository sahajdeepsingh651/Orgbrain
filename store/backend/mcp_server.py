"""Standalone MCP server exposing search_knowledge, get_agent_activity, handoff —
thin wrappers over the exact same functions app/serving.py's REST routes call
(data-passport-core-service.md §5: "one implementation, two protocol faces").

Runs over stdio — the standard local-subprocess MCP transport (Claude Desktop/Claude
Code spawn a server like this and talk to it over stdin/stdout). Identity is resolved
ONCE at startup from MCP_API_TOKEN: there's no per-call HTTP header to carry a bearer
token over stdio, so one server process represents one authenticated session, matching
how these integrations are configured in practice (decisions-log.md, 2026-08-08).
"""

import asyncio
import os

import asyncpg
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

from app.auth import resolve_identity
from app.serving import do_agent_activity, do_handoff, do_search

load_dotenv()

mcp = MCPServer("data-passport")

_pool: asyncpg.Pool | None = None
_identity = None


@mcp.tool()
async def search_knowledge(
    query: str, limit: int = 10, department: str | None = None, team: str | None = None,
) -> list[dict]:
    """Semantic + keyword search across the org's curated knowledge (Data Passport Gold layer)."""
    return await do_search(_pool, _identity, query, limit, department, team)


@mcp.tool()
async def get_agent_activity(team: str | None = None) -> list[dict]:
    """See what any AI agent, anywhere in the org, is currently working on."""
    return await do_agent_activity(_pool, _identity, team)


@mcp.tool()
async def handoff(session_id: str) -> dict:
    """Pick up the full context of a session another agent left off with."""
    try:
        return await do_handoff(_pool, _identity, session_id)
    except LookupError as exc:
        raise ValueError(str(exc)) from exc


async def main():
    global _pool, _identity
    token = os.environ["MCP_API_TOKEN"]
    _identity = resolve_identity(token)
    if _identity is None:
        raise SystemExit("MCP_API_TOKEN is not a known token — check config/api_tokens.json")
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    try:
        await mcp.run_stdio_async()
    finally:
        await _pool.close()


if __name__ == "__main__":
    asyncio.run(main())
