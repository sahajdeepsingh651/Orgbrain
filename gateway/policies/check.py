"""CHECK policy: inspect every request -> redact confidential values ->
send the sanitized request -> restore values in the LLM response.

This is a MINIMAL, test-grade implementation — one hardcoded pattern, not
the real detector suite from docs/ARCHITECTURE.md §5 (regex + entropy + NER for
AWS keys, JWTs, PAN, Aadhaar, etc.). It exists to prove the mechanism
(tokenize before it leaves the gateway, restore before the developer sees
the response), not to replace the Day 2 DLP build.

scan() runs unconditionally on every request — matching §5's "inspect
every request", not gated behind a test flag — but only recognizes the one
pattern below, so it is a no-op in practice except when that exact test
pattern appears.
"""

from __future__ import annotations

import re

from ..protocol.normalized import NormalizedMessage, NormalizedRequest

# A deliberately fake, test-only secret shape — never a real credential
# format. Real detectors (AWS keys, JWTs, PAN, Aadhaar, ...) live in
# pii.py; this single pattern exists to prove the SECRET_/PII_ disjoint-
# prefix merging in app.py and to keep the QA-guide fixture path alive.
_TEST_SECRET_PATTERN = re.compile(r"sk-test-[A-Za-z0-9]{10,}")


def _mint_token(value: str, prefix: str, vault: dict, value_to_token: dict) -> str:
    """Same value -> same token, always, so a repeated secret yields one
    token (matches pii.py's dedup semantics; backported here so the two
    policies have the same reach). This was QA-FINDINGS.md #57's known
    issue — `check.scan` minted fresh tokens per match."""
    token = value_to_token.get(value)
    if token is None:
        token = f"⟦{prefix}_{len(vault) + 1}⟧"
        vault[token] = value
        value_to_token[value] = token
    return token


def _redact_text(text: str, prefix: str, vault: dict, value_to_token: dict) -> str:
    def replace(match: "re.Match[str]") -> str:
        return _mint_token(match.group(0), prefix, vault, value_to_token)

    return _TEST_SECRET_PATTERN.sub(replace, text)


def _redact_blocks(blocks: list[dict], prefix: str, vault: dict, value_to_token: dict) -> list[dict]:
    """Mirror of pii.py's _redact_blocks' recursion contract (same reach,
    different prefix): text blocks + tool_result.content (nested str/list)
    + tool_use.input. A tool_use argument record left in clear text is the
    P2 leak PII fixed internally — keep CHECK's reach level here too."""
    out = []
    for block in blocks:
        block = dict(block)
        if block.get("type") == "text" and "text" in block:
            block["text"] = _redact_text(block["text"], prefix, vault, value_to_token)
        elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            # Walk every string in input.server-side; no field-name gate
            # here since the test-secret pattern is shape-only.
            block["input"] = _redact_json_strings(block["input"], prefix, vault, value_to_token)
        elif "content" in block:
            nested = block["content"]
            if isinstance(nested, str):
                block["content"] = _redact_text(nested, prefix, vault, value_to_token)
            elif isinstance(nested, list):
                block["content"] = _redact_blocks(nested, prefix, vault, value_to_token)
        out.append(block)
    return out


def _redact_json_strings(obj, prefix: str, vault: dict, value_to_token: dict):
    """For CHECK there is no sensitive-field-name list (the test pattern is
    shape-only), so redact ANY string match anywhere in a nested JSON value.
    This matches pii.py's reach without importing its field-name set."""
    if isinstance(obj, dict):
        return {k: _redact_json_strings(v, prefix, vault, value_to_token) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_json_strings(v, prefix, vault, value_to_token) for v in obj]
    if isinstance(obj, str):
        return _redact_text(obj, prefix, vault, value_to_token)
    return obj


def scan(nr: NormalizedRequest) -> tuple[NormalizedRequest, dict]:
    """Walk every message (including nested tool_result content and
    tool_use inputs) and the system prompt; replace matches of the test
    secret pattern with opaque tokens. Returns the (possibly mutated)
    request and a vault mapping token -> real value.

    Empty vault (the common case, when nothing matches) means restore()
    downstream is a no-op — safe to call unconditionally.
    """
    vault: dict = {}
    value_to_token: dict = {}
    prefix = "SECRET"
    new_messages = [
        NormalizedMessage(
            role=m.role,
            content=_redact_blocks(m.content, prefix, vault, value_to_token),
        )
        for m in nr.messages
    ]

    system_context = nr.system_context
    if isinstance(system_context, str):
        system_context = _redact_text(system_context, prefix, vault, value_to_token)
    elif isinstance(system_context, list):
        system_context = _redact_blocks(system_context, prefix, vault, value_to_token)

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


def scan_text(text: str, vault: dict) -> str:
    """Public text-level redactor (see G3): scan a bare string — not a
    NormalizedRequest — against the test-secret pattern. The vault is
    mutated in place so retrieved-context or LLM-draft scanning lands in
    the same token->value map the response restorer already uses. REQUIRED
    for the Q4 ordering fix: retrieved bus documents must scan into the
    existing vault rather than spawning a sibling restorer.

    Stable contract: empty input vault in -> a no-op if nothing matches,
    non-empty vault in -> existing tokens preserved, new matches minted
    on top. Token numbering is len(vault)+1 so collisions with the merged
    PII vault are impossible across disjoint SECRET_/PII_ prefixes."""
    if not _TEST_SECRET_PATTERN.search(text):
        return text
    value_to_token = {v: k for k, v in vault.items() if k.startswith("⟦SECRET_")}
    return _redact_text(text, "SECRET", vault, value_to_token)


def restore(text: str, vault: dict) -> str:
    """Post-hoc restore — correct for a complete string, but NOT safe to
    call per-chunk on a streaming response (a token can split across SSE
    chunk boundaries). Use StreamRestorer for that case."""
    for token, real in vault.items():
        text = text.replace(token, real)
    return text


class StreamRestorer:
    """Boundary-aware restore for text arriving in arbitrary-sized chunks.

    A token can be split across two chunks (e.g. "...⟦SECRET_" arrives in
    one chunk, "1⟧..." in the next). Naively replacing per-chunk would miss
    that match. This holds back the longest suffix that could still be the
    start of some token, and only releases it once it's known not to be
    part of one — the same technique described for restoring redacted
    tokens in docs/ARCHITECTURE.md §5's "streaming gotcha".
    """

    def __init__(self, vault: dict):
        self.vault = vault
        self.buf = ""
        self._max_token_len = max((len(t) for t in vault), default=0)

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        for token, real in self.vault.items():
            self.buf = self.buf.replace(token, real)
        keep = self._partial_suffix_len(self.buf)
        if keep == 0:
            out, self.buf = self.buf, ""
        else:
            out, self.buf = self.buf[:-keep], self.buf[-keep:]
        return out

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return out

    def _partial_suffix_len(self, s: str) -> int:
        longest = self._max_token_len
        if longest == 0 or not s:
            return 0
        for n in range(min(longest - 1, len(s)), 0, -1):
            suffix = s[-n:]
            if any(t.startswith(suffix) for t in self.vault):
                return n
        return 0
