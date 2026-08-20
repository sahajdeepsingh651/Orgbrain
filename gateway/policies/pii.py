"""PII policy: the Day-2 DLP detector suite docs/ARCHITECTURE.md §5 and check.py's
own docstring say hasn't been built yet. check.py exists to prove the
tokenize/restore MECHANISM with one deliberately fake, low-risk pattern
(`sk-test-...`); this module is the first real detector suite on top of
that mechanism.

Real PII shows up in agentic traffic in two distinct shapes, and a detector
that only covers one is missing the more dangerous half:

  1. A human's sentence ("my name is X, my email is Y") — free text, no
     structure to lean on. Regex only.
  2. A tool_result's serialized record (an onboarding lookup, a CRM read,
     ...) — structured JSON where fields like `address` or `full_name`
     have no reliable regex shape at all, but the FIELD NAME says it's
     sensitive regardless of what the value looks like.

Shape 2 is exactly the gap check.py has today: its scan() only walks
blocks with `type == "text"` at the top level of a message, so a
`tool_result` block's nested `content` is never inspected — a value
returned from a tool call sails through untouched. This module walks into
tool_result content explicitly (see _redact_blocks below).

Fill-back reuses check.py's restore() / StreamRestorer as-is — both are
already generic over any token -> value vault, regardless of what
produced it, so nothing new was needed for the response side. See
gateway/app.py for where the two policies' vaults get merged.

Phone numbers use `phonenumbers` (Google's libphonenumber port, `pip
install phonenumbers`) instead of a hand-rolled regex: it's pure Python,
fully offline (no network, no model download — safe for the stub-based
test flow), and actually validates rather than pattern-matches, so it
catches a bare Indian mobile number with no `+91` prefix (a hand-rolled
regex anchored on "91" would miss that) while rejecting digit runs that
merely look phone-shaped. Everything else here stays regex — there's no
equivalent well-maintained library for PAN/Aadhaar, and none of the
alternatives for email/names clear the "actually better, doesn't block the
flow" bar (see PII-PROGRAM.md for the specific tradeoffs considered,
including why NER for names was deferred rather than adopted).

Checksum validators (Verhoeff/Aadhaar, Luhn/card, PAN holder-type digit,
GSTIN mod-36) are adapted from a teammate's separate "Data Passport"
submission — pure-stdlib math, no new dependency, no LLM, no NER, none of
that submission's gazetteer/semantic/risk-scoring/policy-engine machinery.
This module's job is narrower than that one (redact-before-send,
restore-after-receive for a live proxy, not classify-and-score-and-decide
for an offline scanner), and the explicit ask was to keep the connector
lightweight — no model calls of any kind in this path. See
docs/PII-CAPABILITIES.md for what got adopted, what didn't, and why.

A checksum gate changes free-text matching, deliberately: an
Aadhaar/PAN/GSTIN/card-shaped run of characters in prose only gets
redacted if it also passes its checksum, trading a little recall (a
synthetic or malformed number in free text won't match) for a lot of
precision (a random 12-digit order ID won't get redacted as if it were an
Aadhaar). This gate does NOT apply to the JSON-field-name pass below —
`"aadhaar_number": "<anything>"` is redacted on the field name alone,
checksum-valid or not, which is why a fixture's demo/fake ID under a
real field name still redacts correctly.
"""

from __future__ import annotations

import json
import re

import phonenumbers

from ..protocol.normalized import NormalizedMessage, NormalizedRequest

TOKEN_PREFIX = "PII"
_PHONE_REGION = "IN"

# --- checksum validators (adapted from the teammate's deterministic.py) ----

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 7, 2),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _aadhaar_valid(raw: str) -> bool:
    """UIDAI uses the Verhoeff checksum for the Aadhaar check digit."""
    digits = re.sub(r"[\s-]", "", raw)
    if len(digits) != 12 or not digits.isdigit():
        return False
    if digits[0] in "01":            # UIDAI never issues numbers starting 0/1
        return False
    if len(set(digits)) <= 2:        # 111111111111, 121212121212 -> test data
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def _luhn_ok(raw: str) -> bool:
    digits = re.sub(r"[ -]", "", raw)
    if not digits.isdigit():
        return False
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


_PAN_ENTITY_TYPES = set("ABCFGHLJPTKE")  # 4th character encodes PAN holder type


def _pan_valid(raw: str) -> bool:
    return raw.upper()[3] in _PAN_ENTITY_TYPES


_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _gstin_valid(raw: str) -> bool:
    v = raw.upper()
    total = 0
    for i, ch in enumerate(v[:14]):
        idx = _GST_CHARS.index(ch)
        prod = idx * (2 if i % 2 else 1)
        total += prod // 36 + prod % 36
    return _GST_CHARS[(36 - total % 36) % 36] == v[14]


# Free-text regex detectors. Test-grade and deliberately narrow/anchored —
# not a real NER model — matching check.py's own stated scope for this
# layer of the project. Phone numbers are handled separately via
# `phonenumbers` (see module docstring), not by a pattern here. Each entry
# is (pattern, validator); validator is None for patterns with no checksum
# to run (a candidate with a validator only counts if it passes).
_PATTERNS: tuple[tuple[re.Pattern, object], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), None),                                      # email
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE), _pan_valid),                                       # PAN
    (re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b"), _gstin_valid),                # GSTIN
    (re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), None),                                           # IFSC (no checksum exists)
    (re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), _luhn_ok),                                # card (13-19 digits)
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), _aadhaar_valid),                                 # Aadhaar (space, dash, or bare 12 digits — Verhoeff gate filters false positives)
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), None),                                              # ISO date / DOB
    (re.compile(r"(?:[Mm]y name is|[Ii] am|[Ii]'m)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)"), None),  # self-introduced name
)

# Field names redacted wholesale from any embedded JSON object, regardless
# of the value's shape. This is the only way to catch `address` /
# `full_name` — no generic pattern exists for either.
_SENSITIVE_JSON_FIELDS = {
    "full_name", "dob", "pan_number", "aadhaar_number",
    "bank_account", "address", "phone", "email", "emergency_contact", "pan", "pan_card",
    "credit_card", "card_number", "cc_number", "cc",
}
# Note: `_redact_blocks`'s tool_use branch recurses into `input` with this
# field pass. A bare "name" was previously in this set but it redacts tool
# metadata like {"name": "create_ticket"} as if it were a secret — tool_use
# `input` routinely carries a {"name": ...}/{..."name":<model>} key, so it
# was more noise than signal. self-introduced names ("my name is X") are
# still caught by the free-text regex pass below.


def _mint_token(value: str, vault: dict, value_to_token: dict) -> str:
    """Same value -> same token, always. `value_to_token` is the dedup
    ledger for one scan() call; `vault` is the token -> value map callers
    get back. (check.py's own sk-test- detector does NOT dedupe this way —
    see known issue #1 in QA-FINDINGS.md. This module deliberately does.)
    """
    token = value_to_token.get(value)
    if token is None:
        token = f"⟦{TOKEN_PREFIX}_{len(vault) + 1}⟧"
        vault[token] = value
        value_to_token[value] = token
    return token


def _apply_patterns(text: str, vault: dict, value_to_token: dict) -> str:
    """Find every pattern match across the whole text first, then mint
    tokens in left-to-right document order. Doing this pattern-by-pattern
    with sequential .sub() calls would number tokens in *pattern* order
    instead of *appearance* order (e.g. email before an earlier name) —
    wrong whenever a text block mixes entity types, which real messages
    routinely do.
    """
    candidates = []
    for pattern, validator in _PATTERNS:
        for m in pattern.finditer(text):
            if m.lastindex:
                start, end, value = m.start(1), m.end(1), m.group(1)
            else:
                start, end, value = m.start(0), m.end(0), m.group(0)
            if validator is not None and not validator(value):
                continue
            candidates.append((start, end, value))

    for m in phonenumbers.PhoneNumberMatcher(text, _PHONE_REGION):
        candidates.append((m.start, m.end, m.raw_string))

    candidates.sort(key=lambda c: c[0])
    accepted = []
    cursor = -1
    for start, end, value in candidates:
        if start >= cursor:
            accepted.append((start, end, value))
            cursor = end
    if not accepted:
        return text

    out = []
    pos = 0
    for start, end, value in accepted:
        out.append(text[pos:start])
        out.append(_mint_token(value, vault, value_to_token))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _redact_json_value(obj, vault: dict, value_to_token: dict):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _SENSITIVE_JSON_FIELDS:
                if isinstance(v, (dict, list)):
                    # structured value (address: {...}, emergency_contact: {...}) — recurse
                    out[k] = _redact_json_value(v, vault, value_to_token)
                    continue
                coerced = _coerce_str(v)
                if coerced is None:
                    out[k] = v
                else:
                    out[k] = _mint_token(coerced, vault, value_to_token)
            else:
                out[k] = _redact_json_value(v, vault, value_to_token)
        return out
    if isinstance(obj, list):
        return [_redact_json_value(v, vault, value_to_token) for v in obj]
    return obj


def _coerce_str(v) -> str | None:
    """Sensitive values arrive non-string ('phone': 9876543210) or as
    structured containers. Tokenize whatever can be stringified in place
    (a non-string scalar). Containers (dict/list) are handled by the
    RECURSION in _redact_json_value, not here, so nested sensitive keys
    inside address: {...} still match — and None stays None so the field
    lives on the wire as null, not as a 'None' string. Returns False-
    like for non-leaf containers, so callers recurse instead of minting
    a token for the whole object (a previously-silent P3 bug)."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return None


def _redact_text(text: str, vault: dict, value_to_token: dict) -> str:
    """A text block is either a human's sentence (regex only) or an
    embedded JSON record (field-aware redaction first, then regex as a
    defensive second pass over whatever the field list didn't cover)."""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, (dict, list)):
            text = json.dumps(_redact_json_value(parsed, vault, value_to_token), indent=2, ensure_ascii=False)
            return _apply_patterns(text, vault, value_to_token)
    except (json.JSONDecodeError, TypeError):
        pass

    def _replace_json_block(m):
        block_text = m.group(2)
        try:
            parsed = json.loads(block_text)
            if isinstance(parsed, (dict, list)):
                redacted = json.dumps(_redact_json_value(parsed, vault, value_to_token), indent=2, ensure_ascii=False)
                return m.group(1) + redacted + m.group(3)
        except (json.JSONDecodeError, TypeError):
            pass
        return m.group(0)

    # Find JSON blocks wrapped in markdown (```json ... ```)
    text = re.sub(r"(```json\s*\n)(.*?)(\n```)", _replace_json_block, text, flags=re.DOTALL)
    
    def _replace_kv(m):
        val = m.group(2).strip()
        # strip trailing 'and' if present
        if val.lower().endswith(" and"):
            val = val[:-4]
            return m.group(1) + _mint_token(val, vault, value_to_token) + " and"
        return m.group(1) + _mint_token(val, vault, value_to_token)

    for field in _SENSITIVE_JSON_FIELDS:
        # Match field name (optional backticks) followed by is/:/= then the value up to "and", ".", or newline
        pattern = r"(`?" + re.escape(field) + r"`?\s*(?:is|:|=)\s*)(.+?)(?=\s+and\b|\.|\n|$)"
        text = re.sub(pattern, _replace_kv, text, flags=re.IGNORECASE)

    return _apply_patterns(text, vault, value_to_token)

def _redact_blocks(blocks: list[dict], vault: dict, value_to_token: dict) -> list[dict]:
    out = []
    for block in blocks:
        block = dict(block)
        if block.get("type") == "text" and "text" in block:
            block["text"] = _redact_text(block["text"], vault, value_to_token)
        elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            # tool_use: argument JSON carries records">{...}{"full_name":...}.
            # A shared endpoint-tool risk:** arguments leak upstream in clear
            # text even when the same string was redacted elsewhere in the
            # request. Recurse into `input` with the field-name pass.
            block["input"] = _redact_json_value(block["input"], vault, value_to_token)
        elif "content" in block:
            # tool_result (and anything else shaped like it): the nested
            # content is where a tool call's returned record lives — the
            # gap check.py's scan() has today (see module docstring).
            nested = block["content"]
            if isinstance(nested, str):
                block["content"] = _redact_text(nested, vault, value_to_token)
            elif isinstance(nested, list):
                block["content"] = _redact_blocks(nested, vault, value_to_token)
        out.append(block)
    return out


def scan_text(text: str, vault: dict) -> str:
    """Public text-level redactor (see G3): scan a bare string — not a
    NormalizedRequest — against every PII detector here. Same contract as
    check.scan_text: vault mutated in place so retrieved-context or
    LLM-draft scanning lands in the token->value map the response restorer
    already uses. REQUIRED for the G4 ordering fix (scan retrieved bus
    documents before injection) and G6 (DLP-scan the LLM's draft before
    producing sensitivity_flags) — both scan a free string, not a
    NormalizedRequest, so they needed text-level access that scan() does
    not expose."""
    if not text:
        return text
    value_to_token = {v: k for k, v in vault.items() if k.startswith("⟦PII_")}
    return _redact_text(text, vault, value_to_token)


def scan(nr: NormalizedRequest) -> tuple[NormalizedRequest, dict]:
    """Walk every message (including nested tool_result content) and the
    system prompt; redact matches in place. Same contract as
    check.py.scan(): empty vault means nothing matched, safe to no-op
    downstream (restore, StreamRestorer) unconditionally.
    """
    vault: dict = {}
    value_to_token: dict = {}

    new_messages = [
        NormalizedMessage(role=m.role, content=_redact_blocks(m.content, vault, value_to_token))
        for m in nr.messages
    ]

    system_context = nr.system_context
    if isinstance(system_context, str):
        system_context = _redact_text(system_context, vault, value_to_token)
    elif isinstance(system_context, list):
        system_context = _redact_blocks(system_context, vault, value_to_token)

    if not vault:
        return nr, {}

    new_nr = NormalizedRequest(
        model=nr.model,
        system_context=system_context,
        messages=new_messages,
        stream=nr.stream,
        metadata=dict(nr.metadata),
        extra=dict(nr.extra),
    )
    return new_nr, vault
