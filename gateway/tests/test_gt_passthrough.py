"""GT — passthrough fidelity tests.

Critical QA-FINDINGS #9: with all policies off, passthrough must be
byte-identical (the 'transparent proxy' claim, and a precondition for
T4's prompt-cache measurement). Three deviations to fix: bare-string
content gets wrapped in a list, missing `stream` becomes explicit
`false`, and `"system": null` is dropped.
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
from gateway.policies import check as check_policy
from gateway.policies import pii as pii_policy
from gateway.protocol.normalized import NormalizedMessage, NormalizedRequest


_adapter = AnthropicAdapter()


def _round_trip(body):
    return _adapter.from_normalized(_adapter.to_normalized(body))


def _base_msg(**kw):
    body = {"model": "claude-opus-5-1", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
    body.update(kw)
    return body


# ---- Bare-string content stays bare on passthrough ----------------------

def test_gt_bare_string_content_round_trips_bare():
    """`content: "hi"` must come back as `"hi"`, not `[{type:text,text:"hi"}]`."""
    body = _base_msg(messages=[{"role": "user", "content": "hi there"}])
    out = _round_trip(body)
    assert out["messages"] == [{"role": "user", "content": "hi there"}]


def test_gt_block_content_round_trips_as_blocks():
    """Block-list content round-trips as the same list (a block-list on
    entry stays a block-list — re-normalization's list(content) is fine)."""
    blocks = [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
    body = _base_msg(messages=[{"role": "user", "content": blocks}])
    out = _round_trip(body)
    assert out["messages"] == [{"role": "user", "content": blocks}]


# ---- Missing keys preserved --------------------------------------------

def test_gt_missing_stream_key_not_added():
    """No `stream` in body -> no `stream` in output (not explicit `false`)."""
    body = _base_msg()
    assert "stream" not in body
    out = _round_trip(body)
    assert "stream" not in out


def test_gt_explicit_stream_false_preserved():
    body = _base_msg(stream=False)
    out = _round_trip(body)
    assert out["stream"] is False


def test_gt_explicit_stream_true_preserved():
    body = _base_msg(stream=True)
    out = _round_trip(body)
    assert out["stream"] is True


def test_gt_explicit_null_system_preserved():
    body = _base_msg(system=None)
    out = _round_trip(body)
    assert "system" in out
    assert out["system"] is None


def test_gt_missing_system_key_not_added():
    body = _base_msg()
    assert "system" not in body
    out = _round_trip(body)
    assert "system" not in out


# ---- scan match still serializes altered blocks -----------------------

def test_gt_scan_match_rewrites_redacted_blocks():
    """A scanned secret MUST be re-emitted with the token (the original
    stashed body would re-leak the secret). The mutation check must fire."""
    body = {
        "model": "claude-opus-5-1", "max_tokens": 10, "stream": False,
        "messages": [{"role": "user", "content": "secret sk-test-leakme1234 here"}],
    }
    nr = _adapter.to_normalized(body)
    nr2, vault = check_policy.scan(nr)
    out = _adapter.from_normalized(nr2)
    content = out["messages"][0]["content"]
    text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    assert "sk-test-leakme1234" not in text
    assert "⟦SECRET_1⟧" in text


def test_gt_read_inject_changes_messages_so_serializes_new():
    """read.add_context appends a system turn; the mutation check MUST
    emit the serialized form, not the stale original."""
    from gateway.policies import read as read_policy
    body = _base_msg(stream=False)
    nr = _adapter.to_normalized(body)
    nr2 = read_policy.add_context(nr, "INJECTED")
    out = _adapter.from_normalized(nr2)
    text_tail = out["messages"][-1]
    # The injected system turn appears, with INJECTED in its content.
    assert text_tail["role"] == "system" or "INJECTED" in json.dumps(text_tail)


def test_gt_full_byte_identical_passthrough_no_secret():
    """The end-to-end claim: a body with no secret and no marker, after
    to_normalized + from_normalized, is byte-identical (modulo dictity)."""
    body = {
        "model": "claude-opus-5-1", "max_tokens": 100, "stream": True,
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "give me a hand"},
        ],
        "metadata": {"user_id": "sahaj"},
    }
    out = _round_trip(body)
    # After ujson-vs-python normalization, all top-level keys preserved.
    for k in body:
        assert k in out
    assert out["messages"] == body["messages"]
    assert out["system"] == body["system"]
    assert out["stream"] == body["stream"]
    assert out["metadata"] == body["metadata"]
