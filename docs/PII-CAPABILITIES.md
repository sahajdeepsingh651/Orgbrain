# PII detection & redaction — capabilities reference

What the connector's PII layer (`gateway/policies/pii.py`, wired into
`gateway/app.py` alongside `check.py`) actually does today, what it's built
from, and where it's weak. This is a reference snapshot, not a work log —
see `docs/PII-PROGRAM.md` for the chronological build/test history.

A teammate separately sent a much larger "Orgbrain" DLP prototype (zip,
since deleted). This doc also records what was taken from it and what was
deliberately left out, so that decision survives the zip's deletion.

---

## 1. What we used

**Standard library only for the mechanism itself:**
- `re` — all free-text pattern matching.
- `json` — detecting/walking structured records (the `tool_result` JSON case)
  and re-serializing after redaction.

**One third-party library, deliberately:**
- `phonenumbers` (Google's libphonenumber port) for phone number detection —
  pure Python, fully offline, no model, no network call. Chosen over a
  hand-rolled regex because it validates instead of pattern-matching (catches
  a bare Indian mobile with no `+91` prefix; rejects digit runs that merely
  look phone-shaped). Listed in `requirements.txt`.

**Checksum math ported from the teammate's submission** (pure functions, no
new dependency, no model, no network):
- Verhoeff checksum — Aadhaar.
- Luhn checksum — card numbers (new detector; we had none before).
- PAN 4th-character holder-type check — upgrades the existing PAN detector.
- GSTIN mod-36 checksum — new detector (we had no GSTIN coverage before).

**Explicitly NOT used, on request ("no LLMs for PII, keep the connector
lightweight") and on our own earlier design calls:**
- No LLM / model call anywhere in the PII path.
- No NER library (spaCy, Presidio) — see §2.
- No org-vocabulary / gazetteer matching.
- No risk-scoring or destination-policy engine.
- No externally-hosted or API-based detection service of any kind.

---

## 2. What we looked at from the teammate's submission and did NOT take, and why

Their zip (`schema.py`, `pipeline.py`, `policy.py`, `vault.py`, `api.py`,
`detectors/{deterministic,gazetteer,semantic}.py`) is a full 6-layer DLP
*scanner + classifier + policy engine*, not a redact/restore proxy. It solves
a different problem than this gateway does, so most of it doesn't transfer:

| Piece | What it does | Why we left it out |
|---|---|---|
| L4 semantic (`semantic.py`) | Lexicon gate → embeddings → **LLM adjudication** for business-confidential content | Explicit instruction: no LLMs in the PII path. Also a different problem (business-confidential prose, not personal PII). |
| L3b NER (`ner.py`, not even included in the zip) | Presidio/spaCy for names & addresses | A real improvement for name recall, but a model load + synchronous CPU-bound inference inside an `async` request handler blocks the event loop for every concurrent request — conflicts with the gateway's zero-added-latency requirement. Flagged once already this session; still holding off. |
| L3a gazetteer (`gazetteer.py`) | Aho-Corasick match against an org-generated dictionary (client names, project codenames) | Detects *business-confidential* content, not personal PII, and needs nightly-generated dictionary infra we don't have. Out of scope for "redact PII before an LLM call, restore after." |
| Risk scoring + `COMBO_BOOSTS` + `policy.py`'s destination rules + `Passport` artifact | Classifies content, scores re-identification risk, decides block/redact/tokenize **per destination**, stamps an audit artifact | That's a full DLP control-plane product. This gateway does one thing — redact outbound, restore inbound, for one proxied call — not classify-and-decide across six destinations. Building that is a much bigger ask than "keep it lightweight." |
| HMAC `DictVault` (cross-request deterministic tokens, `authorized_actors` gating, `access_log` audit trail) | Same value gets the same token **across requests/sessions**, detokenization gated by actor identity | Nice property, but changes the token contract already agreed and tested (`⟦PII_n⟧`, per-request, reset every `scan()` call — see `PII-PROGRAM.md`). Our gateway is a transparent proxy with no separate "actor" identity to gate rehydration against — the client *is* the trusted developer. |
| Passport/Voter ID/Driving Licence/UPI VPA detectors | Regex-only, no checksum for any of them (their own code doesn't validate these either) | No quality bar to hold them to — adopting would add false-positive surface without the precision win the checksummed types get. Skipped. |
| Partial masking (`XXXXXXXX1234`) | One-way irreversible masking as an alternative to tokenization | Doesn't fit our reversible tokenize→restore model; a different strategy, not asked for. |

## 3. What we adopted from it

Ported into `gateway/policies/pii.py`, as plain functions, verified against
our existing fixture plus new synthetic cases (see §4):

- **Verhoeff checksum** gating the Aadhaar pattern.
- **Luhn checksum** → new card-number detector (13–19 digits, separators
  allowed).
- **PAN holder-type check** (4th character must be one of `ABCFGHLJPTKE`) →
  upgrades the existing PAN pattern.
- **GSTIN mod-36 checksum** → new detector, free text only.
- **IFSC as a standalone free-text pattern** — previously we only caught an
  IFSC code when it sat inside a JSON `bank_account` field; now a bare
  `IFSC HDFC0001234` typed in a chat message is also caught.

---

## 4. Current capabilities — verified and tested

Two independent detection paths, because PII shows up in two shapes in real
agentic traffic:

1. **Free-text regex** (a human's sentence) — entity is redacted only when
   the pattern matches **and**, for checksummed types, the checksum passes.
2. **JSON-field-name pass** (a `tool_result`'s structured record, including
   nested inside `tool_result` blocks — a gap the project's own `check.py`
   still has) — redacted **by field name alone**, regardless of value shape
   or validity. This is the only way to catch `address`/`full_name`, and it
   also means a checksum-invalid value under a recognized field name (e.g. a
   demo/fake Aadhaar tagged `"aadhaar_number"`) still redacts correctly.

| Entity | Path | Verified how |
|---|---|---|
| Person name | Free text (`"my name is X"` / `"I'm X"`), or JSON field (`full_name`, `name`) | Unit test against teammate's HR fixture; live gateway, non-streaming + streaming |
| Email | Free text (regex) or JSON field | Same fixture; dedup confirmed (same address, two occurrences → one token) |
| Phone (IN) | Free text via `phonenumbers`, or JSON field | Same fixture (dedup confirmed) + synthetic case: bare 10-digit number with no `+91` now caught, `12345`-style unrelated digit runs correctly ignored |
| DOB / ISO date | Free text (regex) or JSON field (`dob`) | Fixture |
| PAN | Free text, checksum-gated (holder-type char) | Fixture (valid PAN redacts); ported check adds precision, not separately re-verified against an invalid PAN in this session |
| Aadhaar | Free text, **Verhoeff-gated**; or JSON field (`aadhaar_number`), ungated | Synthetic: valid Aadhaar in free text → redacted; invalid/fake Aadhaar in free text → **not** redacted (documented trade-off, §5); same fake value under `aadhaar_number` field → still redacted |
| GSTIN | Free text, mod-36 checksum-gated | Synthetic case only (`27AAPFU0939F1ZV`) — not exercised via the original fixture, which has none |
| Card number | Free text, Luhn-gated | Synthetic: Luhn-valid card redacted; Luhn-invalid (last digit off by one) correctly **not** redacted |
| IFSC | Free text (new) or JSON field (`bank_account`, wholesale) | Synthetic (free text) + fixture (JSON field) |
| Bank account, address, emergency contact | JSON field only — no generic regex exists for either | Fixture |
| Same value → same token (dedup) | Both paths, single ledger per `scan()` call | Fixture: phone and email each appear twice, both resolve to one token |
| Fill-back (restore) | Reused, unmodified, from `check.py` | Fixture, live gateway, non-streaming |
| Fill-back under streaming, tokens split across SSE chunk boundaries | Reused, unmodified `StreamRestorer` | Live gateway, streaming, stub's default 4-byte chunking |
| No leak to upstream | — | Live gateway: inspected exact bytes the stub upstream received, confirmed no real value present, only tokens |
| No regression from the checksum-porting changes | — | Re-ran the full original fixture (vault, restore, live non-streaming, live streaming) after the change — byte-identical output to before |

---

## 5. Downsides, weaknesses, trade-offs

- **Checksum gate trades recall for precision, in free text only.** An
  Aadhaar/PAN/GSTIN/card-shaped string in prose is only redacted if it also
  passes its checksum. A real, correctly-typed ID will always pass (that's
  what the checksum is *for*); a synthetic, placeholder, or mistyped one in
  free text will not be redacted. The same value under a recognized JSON
  field name is unaffected — it redacts regardless of validity. Net effect:
  fewer false-positive redactions (a random 12-digit order number won't get
  flagged as Aadhaar), at the cost of missing fake/malformed IDs specifically
  when they appear as bare prose.
- **Name and address detection is narrow, not real NER.** Names only match
  a fixed self-introduction phrasing (`"my name is X"` / `"I'm X"`) or a
  recognized JSON field name; a name mentioned any other way (`"ask Rohan
  about this"`, a signature block, a name embedded mid-sentence without that
  exact phrasing) is missed entirely. Addresses have **no free-text
  detector at all** — they are only caught via the JSON-field-name pass.
  NER would close this gap; deliberately not adopted (§2).
- **No cross-request dedup or persistence.** The vault is rebuilt from
  scratch every `scan()` call — the same person's Aadhaar in two separate
  requests gets two different, unrelated tokens. This matches the spec this
  was built against (a per-request vault, never sent to the LLM), but it's a
  real limitation if a future requirement needs referential integrity across
  a conversation spanning multiple gateway calls, or across sessions.
- **No confidence scoring, no review queue, no partial actions.** Every
  match is a binary redact-or-not; there's no equivalent of the teammate's
  low-confidence/quarantine path, no audit trail of what was redacted beyond
  what's in the vault for that one request, and no distinction between
  "definitely PII" and "possibly PII."
- **Phone detection assumes India (`region="IN"`)** — a number from another
  country won't be recognized as a phone number at all (though it may still
  incidentally match nothing and pass through).
- **Narrower Indian-ID coverage than the teammate's submission.** No
  passport number, voter ID, or driving licence detection — skipped because
  none of them have a real checksum to validate against (deliberate
  precision choice, §2), not because they're unimportant.
- **PAN/GSTIN/card detectors are new and only synthetically tested** — the
  original fixture this whole mechanism was built and regression-tested
  against contains no GSTIN or card number, so those two detectors have not
  been exercised against a "real" multi-field record the way Aadhaar/PAN/
  phone/email have.
- **One new runtime dependency** (`phonenumbers`) that didn't exist before
  this work — listed in `requirements.txt`, but anyone running this gateway
  needs to `pip install` it.
- **Business-confidential content is entirely out of scope.** Margins,
  unreleased roadmap, security posture, M&A talk, personnel/HR discussions —
  none of that is detected by this layer at all. The teammate's `semantic.py`
  and `gazetteer.py` were the only pieces of their submission that addressed
  this, and both were left out (§2). If that category of leakage matters for
  this connector, it needs separate work — this doc only covers personal PII.
