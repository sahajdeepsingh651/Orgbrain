"""Bearer-token auth. Each known token maps to a specific caller identity (user_id,
department, team), used both to authenticate ('is this a known token at all') and,
for the serving endpoints, to enforce visibility filtering ('what can this identity see').
Static file, not a real session/SSO system — deliberately no more elaborate than that
for the hackathon (data-passport-core-service.md §3).
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

BASE_DIR = Path(__file__).resolve().parent.parent.parent
TOKENS_FILE = Path(os.environ.get("API_TOKENS_FILE", BASE_DIR / "config" / "api_tokens.json"))

_token_map: dict[str, dict] | None = None


@dataclass(frozen=True)
class Identity:
    user_id: str
    department: str
    team: str | None


def _load_token_map() -> dict[str, dict]:
    global _token_map
    if _token_map is None:
        _token_map = json.loads(TOKENS_FILE.read_text())
    return _token_map


def resolve_identity(token: str) -> Identity | None:
    """Plain token -> Identity lookup, no FastAPI/Request coupling — used by the MCP
    server (backend/app/mcp_server.py), which has no HTTP request to pull a header from."""
    entry = _load_token_map().get(token)
    if entry is None:
        return None
    return Identity(user_id=entry["user_id"], department=entry["department"], team=entry.get("team"))


def require_identity(request: Request) -> Identity:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    identity = resolve_identity(auth[len("Bearer "):])
    if identity is None:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return identity
