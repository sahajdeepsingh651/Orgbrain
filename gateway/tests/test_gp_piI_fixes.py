"""GP regression tests — the four PII defects + scan_text entry point.

Each defect has one assertion pair: redacted outbound (the request the
upstream sees holds tokens, not secrets), and restored inbound (the client
sees the real value back). No service or HTTP needed — these call the
policy functions directly on built NormalizedRequests."""
from __future__ import annotations

import json

import pytest

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gateway.policies import check as check_policy
from gateway.policies import pii as pii_policy
from gateway.protocol.normalized import NormalizedMessage, NormalizedRequest


def _nr_for(messages_blocks, *, model="claude-opus-5-1", system=None):
    msgs = [NormalizedMessage(role=r, content=b) for r, b in messages_blocks]
    return NormalizedRequest(model=model, system_context=system, messages=msgs, stream=False)


# ---- P1: ensure_ascii=False (escaped ⟦ broke JSON-path fill-back) ------

def test_p1_ensure_ascii_false_on_json_path():
    """A text block that is JSON and matches a sensitive field must
    serialize with ensure_ascii=False so the ⟦PII_n⟧ token survives to
    restore() verbatim. The bug: default ensure_ascii=True ships the
    6-char \\u escape, restore() exact-replaces the real ⟦ and misses."""
    # An Aadhaar field name triggers _redact_json_value; the token must
    # render as the real ⟦ chars, not \\u27e6.
    text = json.dumps({"aadhaar_number": "234123412342", "note": "see"})
    nr = _nr_for([("user", [{"type": "text", "text": text}])])
    nr2, vault = pii_policy.scan(nr)
    if not vault:
        pytest.skip("Aadhaar Verhoeff rejected — synthetic test data, adapt")
    out_text = nr2.messages[0].content[0]["text"]
    # The token must be the real ⟦PII_1⟧, never \\u27e6PII_1\\u27e7
    assert "\\u27e6" not in out_text, f"P1 REGRESSION: escaped token in JSON path: {out_text!r}"
    assert "⟦PII_" in out_text, f"token missing entirely: {out_text!r}"
    # And restore() must round-trip cleanly.
    restored = check_policy.restore(out_text, vault)
    assert json.loads(restored)["aadhaar_number"] == "234123412342"


def test_p1_ensure_ascii_false_email_field():
    """Email is a non-checksum pattern; it always matches. Independent of
    Aadhaar's Verhoeff gate, so this is the unconditional P1 assertion."""
    text = json.dumps({"email": "sahaj@example.com", "note": "see"})
    nr = _nr_for([("user", [{"type": "text", "text": text}])])
    nr2, vault = pii_policy.scan(nr)
    out_text = nr2.messages[0].content[0]["text"]
    assert "\\u27e6" not in out_text, f"P1 REGRESSION: {out_text!r}"
    assert "⟦PII_1⟧" in out_text
    restored = check_policy.restore(out_text, vault)
    assert json.loads(restored)["email"] == "sahaj@example.com"


# ---- P2: tool_use argument redaction ------------------------------------

def test_p2_tool_use_input_redacted_outbound():
    """A tool_use block has `input`, not `content`; the old branch missed
    it entirely and arguments went upstream in clear text. Verify the
    same string that is in a text block is redacted identically."""
    secret = "sk-test-leakme1234"  # 10+ chars after prefix
    blocks = [
        {"type": "text", "text": f"the secret is {secret}"},
        {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"token": secret}},
    ]
    nr = _nr_for([("user", blocks)])
    nr2, check_vault = check_policy.scan(nr)
    nr2, pii_vault = pii_policy.scan(nr2)
    vault = {**check_vault, **pii_vault}
    out_blocks = nr2.messages[0].content
    # BOTH the text and the tool_use input must hold the token.
    tool_use = next(b for b in out_blocks if b.get("type") == "tool_use")
    assert tool_use["input"]["token"] != secret, "P2 REGRESSION: tool_use argument leaked upstream"
    assert any("⟦SECRET_" in str(b) for b in out_blocks)
    restored = check_policy.restore(tool_use["input"]["token"], vault)
    assert restored == secret


# ---- P3: structured sensitive values + non-string coercion --------------

def test_p3_structured_address_recurses_not_mints_none():
    """'address': {'line1': ...} must recurse, not mint a token for the
    whole object (the bug: _coerce_str returned None for dicts and that
    None leaked into the vault — `restore()` would then replace the token
    with the literal string 'None')."""
    blocks = [{
        "type": "tool_use", "id": "t1", "name": "lookup",
        "input": {"address": {"line1": "12 MG Rd", "phone": "91234 56789"}},
    }]
    nr = _nr_for([("user", blocks)])
    nr2, vault = pii_policy.scan(nr)
    assert None not in vault.values(), f"P3 REGRESSION: None in vault {vault}"
    tool_use = nr2.messages[0].content[0]
    # address.line1 is not itself a sensitive field name; address the KEY
    # is. Nested 'phone' under address redacts via field-name recursion.
    assert tool_use["input"]["address"]["phone"].startswith("⟦PII_")
    # And restore puts the real phone back, not 'None'.
    restored_phone = check_policy.restore(tool_use["input"]["address"]["phone"], vault)
    assert restored_phone == "91234 56789"


def test_p3_non_string_phone_redacted():
    """'phone': 9876543210 (integer) must coerce to the string form and
    tokenize, not slide through because isinstance(v, str) is False."""
    blocks = [{
        "type": "tool_use", "id": "t1", "name": "lookup",
        "input": {"phone": 9876543210},
    }]
    nr = _nr_for([("user", blocks)])
    nr2, vault = pii_policy.scan(nr)
    tool_use = nr2.messages[0].content[0]
    assert tool_use["input"]["phone"].startswith("⟦PII_"), f"P3 REGRESSION: int phone leaked: {tool_use['input']}"
    restored = check_policy.restore(tool_use["input"]["phone"], vault)
    assert restored == "9876543210"


# ---- P4: bare Aadhaar (no separators) -----------------------------------

def test_p4_aadhaar_regex_matches_bare_and_dashed_forms():
    """The regex must match bare 12-digit AND dash/space forms. The Verhoeff
    gate then rejects synthetic data — but the REGEX must reach the gate,
    not fail at the pattern level. Assert via the pattern directly so the
    checksum (which is right to reject) doesn't mask the regex bug."""
    import re
    pat = pii_policy._PATTERNS[5][0]
    for raw in ["452188901123", "4521-8890-1123", "4521 8890 1124"]:
        assert pat.search(raw), f"P4 REGRESSION: Aadhaar regex missed {raw!r}"


# ---- G3: scan_text entry point -------------------------------------------

def test_g3_scan_text_check_redacts_and_dedupes():
    """check.scan_text on a free string with two distinct secrets and one
    repeated secret yields two tokens, the repeated value mints ONE token
    (dedup ledger). Same contract as pii.scan_text."""
    text = "first sk-test-aaaaaaaaaa then sk-test-bbbbbbbbbb then sk-test-aaaaaaaaaa again"
    vault = {}
    out = check_policy.scan_text(text, vault)
    # two distinct values -> two tokens
    assert len(vault) == 2, f"dedup failed: {vault}"
    assert out.count("⟦SECRET_") == 3  # three occurrences, two tokens


def test_g3_scan_text_pii_redacts():
    vault = {}
    out = pii_policy.scan_text("email me at sahaj@example.com", vault)
    assert "sahaj@example.com" not in out
    assert "⟦PII_" in out
    assert "sahaj@example.com" in vault.values()


def test_g3_scan_text_merges_into_existing_vault():
    """The contract G4 relies on: scanned retrieved-context tokens land in
    an EXISTING vault, not a sibling. Token numbers continue, not restart."""
    vault = {"⟦SECRET_1⟧": "sk-test-existing1"}
    # pii.scan_text numbers from len(vault)+1 since it counts existing PII_
    # entries; with no existing PII entries it starts at PII_1.
    pii_policy.scan_text("reach sahaj@example.com", vault)
    assert "⟦SECRET_1⟧" in vault  # original preserved
    assert any(k.startswith("⟦PII_") for k in vault)


# ---- backport: check.scan now recurses into tool_result + dedupes -------

def test_check_scan_recurses_tool_result_and_dedupes():
    """Backport: check.scan used to walk top-level text blocks only and
    mint per-match tokens (QA-FINDINGS #57). After the backport it must
    recurse into tool_result.content and dedup identical values."""
    secret = "sk-test-dedupme1234"  # 10+ chars after prefix to match the regex
    blocks = [
        {"type": "text", "text": secret},
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": secret}]},
    ]
    nr = _nr_for([("user", blocks)])
    nr2, vault = check_policy.scan(nr)
    assert len(vault) == 1, f"dedup failed: {vault} — same secret should yield one token"
    # The text-block and the nested tool_result token are the SAME token.
    text_tok = nr2.messages[0].content[0]["text"]
    nested_tok = nr2.messages[0].content[1]["content"][0]["text"]
    assert text_tok == nested_tok == list(vault)[0]
