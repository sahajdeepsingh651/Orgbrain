"""G1 — session/account identity in the adapter.

Unit tests against the SANITISED sample fixtures (two share a session_id, one
differs) — same wire shape as a real capture, synthetic identifiers. Real
captures in fixtures/ are gitignored and must never be relied on by a test,
or it passes locally and fails on a fresh clone.
plus the negative cases the plan specified: no metadata, non-JSON user_id,
plain-string user_id, all yield None without raising; the wire `metadata`
key round-trips byte-for-byte via `extra`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest

from gateway.protocol.anthropic_adapter import AnthropicAdapter
from gateway.policies import identity


_adapter = AnthropicAdapter()


def _load_fixture(name: str) -> dict:
    p = _ROOT / "fixtures" / name
    return json.loads(p.read_text())


FIXTURES = sorted(Path(_ROOT / "fixtures").glob("sample_*_v1_messages.json"))


def test_g1_real_fixtures_parse_session_id():
    """Two of three real fixtures share a session_id; the third differs.
    All three parse the same account_uuid (same machine)."""
    sessions = []
    accounts = []
    for fp in FIXTURES:
        body = _load_fixture(fp.name)
        nr = _adapter.to_normalized(body)
        assert nr.metadata["protocol"] == "anthropic"
        assert "session_id" in nr.metadata
        assert "account_uuid" in nr.metadata
        sessions.append(nr.metadata["session_id"])
        accounts.append(nr.metadata["account_uuid"])
    # Two sessions shared, one different — assert exactly the right pattern.
    assert sessions.count("11111111-0000-4000-8000-0000000000a1") == 2
    assert any(s == "22222222-0000-4000-8000-0000000000b2" for s in sessions)
    # All three fixtures share the same account_uuid (same machine).
    assert len(set(accounts)) == 1
    assert accounts[0] == "aaaaaaaa-0000-4000-8000-000000000001"


def test_g1_missing_metadata_returns_none():
    body = {"model": "claude-opus-5-1", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10, "stream": False}
    nr = _adapter.to_normalized(body)
    assert "session_id" not in nr.metadata
    assert "account_uuid" not in nr.metadata
    assert nr.metadata["protocol"] == "anthropic"


def test_g1_non_json_user_id_returns_none():
    """A plain-string user_id (not the JSON envelope Claude Code sends)
    must yield None, not raise."""
    body = {
        "model": "claude-opus-5-1", "max_tokens": 10, "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": "just-a-plain-string"},
    }
    nr = _adapter.to_normalized(body)
    assert "session_id" not in nr.metadata
    assert "account_uuid" not in nr.metadata


def test_g1_dict_user_id_returns_none():
    """If user_id is sent as a nested object rather than a JSON string,
    parse silently returns None — no TypeError."""
    body = {
        "model": "claude-opus-5-1", "max_tokens": 10, "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": {"session_id": "x"}},
    }
    nr = _adapter.to_normalized(body)
    assert "session_id" not in nr.metadata


def test_g1_round_trip_preserves_metadata_byte_for_byte():
    """The wire `metadata` key round-trips via `extra` — adding session ref
    to gateway-internal metadata MUST NOT change what is sent upstream."""
    body = {
        "model": "claude-opus-5-1", "max_tokens": 10, "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": json.dumps({"device_id": "d", "account_uuid": "aaaaaaaa-0000-4000-8000-000000000001", "session_id": "11111111-0000-4000-8000-0000000000a1"}), "other": "keep-me"},
    }
    nr = _adapter.to_normalized(body)
    out = _adapter.from_normalized(nr)
    # The `metadata` key on the wire MUST be byte-identical to what was sent.
    assert "metadata" in out
    assert out["metadata"] == body["metadata"]
    # And the internal session/account were derived (gateway-only).
    assert nr.metadata["session_id"] == "11111111-0000-4000-8000-0000000000a1"
    assert out["metadata"]["user_id"] == body["metadata"]["user_id"]


# ---- identity.py policy mapping ----

def test_identity_resolve_known_account():
    identity.reload_map()
    bid = identity.resolve("aaaaaaaa-0000-4000-8000-000000000001")
    assert bid is not None
    assert bid.bus_token == "test-token-platform"
    assert bid.user_id == "u-dev"
    assert bid.department == "Engineering"
    assert bid.team == "platform"


def test_identity_resolve_unknown_returns_none():
    identity.reload_map()
    assert identity.resolve("does-not-exist-uuid") is None
    assert identity.resolve(None) is None
    assert identity.resolve("") is None


def test_identity_account_hash_truncates():
    """Logging must not leak the full account_uuid."""
    h = identity.account_hash("aaaaaaaa-0000-4000-8000-000000000001")
    assert h.endswith("…")
    assert len(h) == 11  # 10 chars + ellipsis
    assert "aaaaaaaa" in h
    assert "aca299ba" not in h  # no tail leakage
    assert identity.account_hash(None) == ""
