# PII redaction/restore program — work log

Source task: teammate handed over a realistic `/v1/messages` request body plus
its expected LLM response, deliberately shaped to put PII in the two places it
actually shows up in agentic traffic — a human's sentence, and a tool_result's
serialized record — and asked for a program that redacts on the way out and
fills the tokens back in on the way back, with same-value-same-token dedup and
(if in scope) boundary-safe streaming restore.

This doc tracks what was built, how it was tested, what broke during testing,
and the final outcome. Update this file, don't create a second one, as the
work continues.

---

## What was built

**New file:** [`gateway/policies/pii.py`](../gateway/policies/pii.py)

A new detector suite, structured the same way as `check.py` (`scan()` /
reuses `check.py`'s `restore()` and `StreamRestorer` unchanged for fill-back).
Two detection layers, matching the two places PII actually appears:

1. **Free-text regex** — email, Indian mobile number, PAN, Aadhaar, ISO date,
   and a `"my name is X"` / `"I'm X"` self-introduction pattern. Runs on any
   text block that isn't structured JSON.
2. **JSON-field-aware redaction** — when a text block parses as JSON (this is
   how a `tool_result`'s returned record actually looks on the wire), a fixed
   list of sensitive field names (`full_name`, `address`, `bank_account`,
   `dob`, `pan_number`, `aadhaar_number`, `phone`, `email`,
   `emergency_contact`, `name`) gets redacted **wholesale by field name**,
   regardless of what the value looks like. This is the only way to catch
   `address` — there is no generic address regex.

**Dedup:** a single `value -> token` ledger is threaded through one whole
`scan()` call, so the same string always gets the same token no matter which
layer or which block found it first. This was an explicit requirement (the
phone number and email in the fixture each appear twice) and is something
`check.py`'s own `sk-test-` detector does **not** do (see known issue #1 in
`QA-FINDINGS.md`) — `pii.py` deliberately does not repeat that.

**Token minting order = left-to-right document order,** not pattern-priority
order. Doing per-pattern sequential substitution instead (try email
everywhere, then phone everywhere, etc.) numbers tokens in the wrong order
whenever one text block mixes entity types — which the fixture's first
message does (name before email before phone). Fixed by collecting all
pattern matches first, sorting by position, then minting.

**Modified file:** [`gateway/app.py`](../gateway/app.py) — `pii_policy.scan()`
now runs right after `check_policy.scan()`, and the two vaults are merged
(disjoint token prefixes, `SECRET_` vs `PII_`, so no collision risk). No other
pipeline code changed — `restore()` and `StreamRestorer` are already generic
over any vault dict, so the response side needed zero new code.

---

## A gap this surfaced in the existing code

`check.py`'s `scan()` only walks blocks with `type == "text"` **at the top
level of a message**. A `tool_result` block's nested `content` list is never
inspected, so a secret or PII value returned from a tool call sails through
`check.py`'s detector completely untouched. `pii.py` walks into `tool_result`
content explicitly (`_redact_blocks`'s `elif "content" in block` branch,
recursing into whatever's nested there). Worth deciding whether to backport
the same recursion into `check.py` itself — right now the two policies have
different blind spots for the same shape of traffic.

---

## Test fixture (the teammate's example, reproduced exactly)

Request (abbreviated — full body has the standard `system`/`messages`
envelope around this): a user message with a name/email/phone in free text, a
`tool_use` call, and a `tool_result` whose `content` is a JSON-encoded
employee record with 10 fields, 4 of which are duplicates of values already
seen in the free-text message (name, phone, email — and `employee_id`, which
is deliberately **not** treated as PII).

Expected vault, per the teammate's spec:

```
PII_1 = Rohan Mehta               PII_6 = 4521 8890 1123 (Aadhaar)
PII_2 = rohan.mehta87@gmail.com   PII_7 = IFSC: HDFC0001234, A/C: 50100234567890
PII_3 = +91 98765 43210           PII_8 = <address field, verbatim>
PII_4 = 1997-03-14 (DOB)          PII_9 = Priya Mehta, +91 91234 56789
PII_5 = BQXPM4521K (PAN)
```

## Test methodology

1. Unit-level: built the exact request as valid JSON (the pasted version had
   embedded literal newlines that aren't valid JSON — reconstructed
   programmatically instead of hand-escaping), ran `pii.scan()` directly, and
   diffed the resulting vault against the expected one above.
2. Fill-back: built the teammate's exact LLM response fixture (still full of
   `⟦PII_n⟧` tokens) and ran `check_policy.restore()` against the vault from
   step 1.
3. End-to-end, non-streaming: same request through the real
   `gateway/app.py` + stub upstream (the harness from the earlier QA pass) —
   confirmed the stub only ever received tokens, never real values, and the
   client-facing response had everything restored.
4. End-to-end, streaming: same request with `"stream": true` and
   `DP_CHECK_RESTORE_STREAM=1`, through the real gateway with the stub's
   default 4-byte SSE chunking (small enough to split multi-character tokens
   across chunk boundaries) — confirmed `StreamRestorer` (already existing,
   unmodified) reassembles correctly with **zero PII program code**, since it
   was written generic over any vault from the start.
5. Regression: re-ran two checks from the earlier QA pass (`sk-test-` secret
   redact+restore, READ injection gating) against the gateway with `pii.py`
   wired in, to confirm the new policy doesn't interfere with the existing
   one.

## Outcome

| Check | Result |
|---|---|
| Vault matches expected `PII_1`..`PII_9`, exact values | Pass, after 2 fixes (below) |
| Dedup — phone (`PII_3`) and email (`PII_2`) each minted once, reused | Pass |
| Token numbering follows document order, not pattern order | Pass |
| `employee_id` correctly left un-redacted (not PII) | Pass |
| Fill-back restores every token, including the duplicated phone | Pass |
| End-to-end through real gateway, non-streaming | Pass |
| End-to-end through real gateway, streaming, tokens split across SSE chunks | Pass — `StreamRestorer` needed no changes |
| Regression: `sk-test-` detector + READ injection still work with `pii.py` wired in | Pass |

### Bugs found and fixed during this work (in the new code, before it ever ran clean)

1. **Name regex swallowed the rest of the sentence.** The self-introduction
   pattern used `re.IGNORECASE` on the whole compiled regex, which makes
   `[A-Z]`/`[a-z]` match *any* case under Python's `re` — so the "must start
   with a capital letter" heuristic silently stopped meaning anything, and
   the pattern greedily consumed run-on lowercase text after the real name
   (`"Rohan Mehta and my email is rohan"`, `"trying to update my emergency
   contact number to"`). Fixed by scoping case-insensitivity to just the
   intro phrase (`[Mm]y name is|[Ii] am|[Ii]'m`) and keeping the name capture
   case-sensitive.
2. **Email regex ate trailing sentence punctuation.** `[\w.-]+` as the final
   domain segment happily matches a trailing full stop, so
   `"...gmail.com. I'm trying..."` redacted to `rohan.mehta87@gmail.com.`
   (with the period) in free text but `rohan.mehta87@gmail.com` (without) in
   the JSON field — two different strings, so dedup silently failed and the
   same email got two tokens. Fixed by requiring each domain label after a
   dot (`(?:\.[\w-]+)+`), so the match stops cleanly at `.com`.

### Answering the teammate's specific questions

- **Streaming, if in scope:** it's in scope for the gateway generally, and
  the existing `StreamRestorer` in `check.py` already handles it correctly
  for PII tokens with no changes — confirmed above, not just asserted. No
  need to hand over a separate implementation; it's already shared.
- **Dedup by value:** done, verified against the fixture's two duplicated
  values (phone, email).

---

## Update: ported checksum validators from a teammate's separate submission

A teammate sent a much larger "Orgbrain" DLP prototype (6-layer scanner
+ risk scoring + destination policy engine + LLM semantic layer). Reviewed
it, kept only the lightweight, checksum-based pieces per instruction ("take
what's better, leave the rest," "no LLMs for PII, keep the connector
lightweight"): Verhoeff (Aadhaar), Luhn (new card-number detector), PAN
holder-type check, GSTIN mod-36 checksum (new detector), plus a standalone
free-text IFSC pattern. Explicitly did not adopt their LLM layer, NER,
gazetteer, or policy/scoring engine — those solve a different problem
(classify-and-decide) than this module's (redact-and-restore). Re-ran the
full original fixture after the change — byte-identical output to before.
Full rationale, adoption table, and the resulting capability/limitation list
now live in `../PII-CAPABILITIES.md` (root of the project) rather than here,
since that's a reference snapshot rather than a work-log entry.

### Known limitation carried over from `check.py`

- Regex-only entity detection (no real NER) — same test-grade scope
  `check.py` already documents for itself. Name/address detection in
  particular will miss shapes not covered by the patterns above (e.g. a name
  not preceded by "my name is" / "I'm").

---

## Library pass: swapped the phone regex for `phonenumbers`

Asked to use a proper library anywhere it's a genuine improvement and doesn't
block the flow. Went through each detector:

| Entity | Library considered | Verdict |
|---|---|---|
| Phone (IN) | `phonenumbers` (Google libphonenumber port) | **Adopted** |
| Email | `email-validator` / similar | Not adopted — see below |
| PAN / Aadhaar | none exist | Stayed regex — nothing to swap to |
| Name | spaCy / Presidio (NER) | Considered, not adopted yet — see below |

**Adopted `phonenumbers` for phone detection.** Pure Python, no network
calls, no model download — fully compatible with the offline stub-based test
flow and doesn't add latency risk (parsing is cheap, not ML inference).
Genuinely better than the regex it replaced, not just "more official":

- **Catches numbers the regex missed.** The old pattern
  (`\+?91[\s-]?\d{5}[\s-]?\d{5}`) required something matching a `91` prefix.
  A bare Indian mobile with no country code — `"call me on 98765 43210"` —
  matched *nothing*. `phonenumbers.PhoneNumberMatcher(text, "IN")` catches it
  via the region hint.
- **Avoids false positives the regex was blind to.** It validates
  (`Leniency.VALID` is the matcher's default), so an unrelated 5-digit number
  next to a real one (tested: `"...or 12345 is my locker code"`) isn't
  swept in — a digit-counting regex has no way to tell the two apart.
- Verified: re-ran the full fixture (unit vault, fill-back, live
  non-streaming, live streaming) after the swap — identical, correct output
  in every case. `requirements.txt` added (the project had none) so this
  doesn't silently break on a fresh checkout.

**Not adopted for email.** The regex already reliably catches
email-*shaped* strings, which is all redaction needs; an email-validation
library's real value-add (deliverability, RFC-edge-case parsing) isn't
relevant to a DLP pass. Not a clear enough win to add a dependency for.

**PAN / Aadhaar** have no equivalent well-maintained library — these are
India-specific ID formats, not something libphonenumber-style projects
cover. Regex is the only real option here.

**Name detection via NER (spaCy / Presidio) — considered, held off.** This
is the one place a real library would clearly out-detect the current regex
(catches names regardless of "my name is" phrasing). Didn't pull it in
because it's a materially different kind of dependency than `phonenumbers`:
a spaCy model needs a separate download step, adds real startup cost as the
model loads, and — more importantly — `nlp(text)` is synchronous, CPU-bound
inference running inside an `async def` request handler, which blocks the
event loop for every other in-flight request on this same gateway process
for the duration of the call. That's a direct conflict with the project's
own north-star requirement (see `TEST-PLAN.md`/`QA-TEST-GUIDE.md`'s
transparency section): the gateway must add near-zero latency and never
stall concurrent streams. It's also explicitly scoped as "Day 2, real NER"
future work in `ARCHITECTURE.md` §5 and `check.py`'s own docstring, not
something assumed to ship now. Flagging this as a decision for the team
rather than making the call unilaterally — happy to prototype it behind a
flag if wanted.
