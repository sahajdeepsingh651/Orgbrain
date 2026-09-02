# Orgbrain — Egress Gate (Endpoint Security)

> Status: Interception mechanism (§5) still **PROPOSED, not confirmed**. Policy configuration model (§3) and interception/extraction mechanics (§4) reflect team decisions made 2026-08-07 — see `decisions-log.md`. Updated 2026-08-07: what this doc originally called the "Ingestion Gate" (a separate, server-side checkpoint) has been merged into this one — PII detection/redaction happens in exactly one place, the endpoint device, for every outbound flow.

## 1. What this is and why it exists

PII/credential detection and redaction happens in exactly one place: **the endpoint device**, before anything leaves it. One shared on-device engine is invoked for two different outbound flows, each with its own destination policy:

1. **Toward Orgbrain's own ingest API** — always redact, no exceptions. Covered in `orgbrain-architecture.md` § The Endpoint Checkpoint. This protects the org's own knowledge base from ever holding raw PII, even internally — no server-side scan, no exemption for "it's our own infrastructure."
2. **Toward any external AI** — the Egress Gate proper, the rest of this doc: destination-based policy (§2), which does distinguish local/on-prem from external SaaS. (Same "one shared engine, many callers" idea as §5 Option D below — extended here to cover the ingest flow too, not just the AI-egress interception channels.)

This is the other half of the project's thesis: not just "make knowledge travel," but "stop the data that shouldn't travel, at the earliest possible point" — which turned out to be the same point for both flows.

One engine, two flows:

| | Toward Orgbrain (ingest) | Toward external AI (egress) |
|---|---|---|
| Location | Endpoint device, before `POST /v1/ingest` | Endpoint device, before any outbound AI call |
| Protects | The shared knowledge base | The organization's data leaving to external AI |
| Trigger | A session gets captured into the passport | A user/agent sends a prompt to any AI, anywhere |
| Destination policy | Always redact — no exemptions, since this is a persistent store shared broadly across the org | Default-deny by destination — local/on-prem LLM exempt, external SaaS redacted/blocked (§2) |

## 2. Policy: what gets blocked, and where

**Destination-based rule:**
- Local / on-premise LLM → allowed through unmodified. Sensitive content can be used freely here because it never leaves the org's infrastructure.
- Any other AI endpoint (public SaaS APIs — OpenAI, Anthropic public API, Gemini, ChatGPT/Claude/Gemini web UIs, third-party AI features embedded in other SaaS tools) → PII and confidential data get blocked or redacted before the request leaves the device.

**Default-deny, not a blocklist.** Maintain an allowlist of known-safe destinations (internal LLM gateway, localhost, approved internal domains). Anything not on the allowlist is treated as external and subject to redaction/blocking. A blocklist of "known AI SaaS domains" would require constant updating as new AI products launch; default-deny doesn't have that maintenance burden.

**Content categories to detect:**
1. **PII** — regex + NER: emails, phone numbers, names, customer IDs. Same on-device detection engine used for the ingest-bound flow (§1) — not a separate implementation.
2. **Credentials/secrets** — API keys, tokens, connection strings (gitleaks-style pattern matching).
3. **Confidential business data** (checked only for this AI-bound flow — content bound for Orgbrain's own ingest API doesn't need this check; internal confidential business knowledge, gated by `visibility`, is exactly what the knowledge base is for):
   - Literal prices / currency figures in text (regex: currency symbols, "$X,XXX", "quote of", "margin of X%").
   - Financial and pricing documents — keyword/heuristic matching ("price list," "quote," "P&L," "confidential — pricing") as a first pass.
   - Optional, higher-precision: document fingerprinting — hash known confidential documents once (e.g., the actual price list, financial statements) and flag near-exact matches later. Catches copy-pasted content that keyword matching would miss, at the cost of needing a maintained corpus of fingerprinted docs.

**On a match:** block the request (hard stop, user sees why) or redact-and-continue (strip the flagged spans, let the rest through), depending on category severity — credentials and exact document fingerprint matches probably warrant a hard block; loose price-figure matches might warrant redact-and-continue with a warning. This threshold is a policy decision, not a technical one — worth a product/security call, not just an engineering default.

## 3. Policy configuration model — who decides what gets checked

Decided 2026-08-07: neither "the system silently checks everything" nor "the employee fully controls what's checked" — a two-tier model, applied consistently to both the destination scope and the data-category list:

- **Admin policy sets a mandatory floor.** The security/admin team defines what's *always* checked, org-wide, non-negotiable by the individual employee — e.g. "PII and credential scanning is always on, for every external destination" and/or "these specific known AI SaaS domains are always monitored."
- **Employee choice extends the floor, never shrinks it.** An employee can add more destinations to watch (e.g. a niche AI tool the admin list doesn't cover yet) or more categories to check on their own content — but cannot exempt a destination or category the admin floor already mandates.

This keeps the "foolproof" property for whatever the org decides is non-negotiable, while giving employees real say (and reducing the "my employer silently wiretaps my whole device" objection) everywhere the org hasn't drawn a hard line.

The same two-tier shape governs the **third, separate decision** of which sessions get linked into the shared knowledge lakehouse at all (not an Egress Gate concern — that's Orgbrain's own consent model, see `orgbrain-architecture.md` § Consent model). Same pattern, different checkpoint: admin-mandated categories auto-capture, everything else requires the employee's own opt-in action.

## 4. Interception & extraction mechanics — how this actually works

Three distinct mechanisms exist, with different reach. "Install software on the device" solves *coverage*; it does not by itself solve *understanding what's in the traffic* — that distinction matters for scoping the build.

### 4.1 Path 1 — MCP-native tools (strongest, but narrowest by default)
> **SUPERSEDED (2026-08-09): MCP is READ-ONLY.** `record_insight` is not built and must not be — writes go AI session -> Interceptor -> Context Bus, so that the human approval gate and the DLP boundary cannot be bypassed. An MCP server is not in the payload path (the harness executes tool calls locally; they never reach `api.anthropic.com`), so it can never see the `tool_result` where a credential actually lands. MCP remains a useful *read* surface for clients the gateway cannot reach — accepted trade-off: those reads bypass the interceptor's audit log.

For any AI tool that speaks MCP (Claude Code, and anything else we wire up), retrieval needs no interception: the tool calls `search_knowledge` / `get_agent_activity` / `handoff` directly. Structured data, no parsing/guessing, works identically on every OS since MCP runs over stdio/HTTP. Covers only MCP-speaking tools — doesn't touch a browser tab open to a chat UI.

### 4.2 Path 2 — device-level network proxy (the "software on the device" approach)
The agent sets itself as the system's HTTP(S) proxy (or redirects traffic to itself at the OS network layer) and installs a locally-trusted root certificate to decrypt TLS traffic.

- **This plumbing is genuinely OS-specific** — Windows, macOS, and Linux each need their own implementation of "become the system proxy + install a trusted CA + redirect traffic." There's no single cross-platform trick; budget it as three separate small systems-programming efforts, not one.
- **What it buys you:** device-wide coverage — sees any HTTPS traffic regardless of which app/browser sent it, no per-app integration.
- **What it does NOT automatically buy you:** decrypting the bytes doesn't mean knowing where the prompt text lives inside the JSON. Every AI service (OpenAI's API shape, Anthropic's, each web UI's form post) is a different wire format. Reading or redacting actual content still needs a small parser per service — the proxy removes the need to touch each app's *UI*, not the need to understand each service's *data shape*.
- **The scope-limiting fallback:** for any destination without a written parser, do coarse domain-level allow/block only (no content inspection). Fine-grained redact-vs-block-by-severity only exists for the handful of services someone bothered to parse. This is also exactly what §3's admin-mandated destination list is for — it's the list of domains worth writing a parser for.
- **A real limitation, not a bug to hide:** some apps pin their TLS certificate and will refuse to trust the proxy's certificate — interception simply fails there, and the connection breaks rather than being silently inspected. That's a safe failure mode (fails closed, nothing leaks silently), but "we intercept everything" isn't literally true; it's "we intercept everything that doesn't pin its cert, and hard-block what we can't inspect."
- **Same proxy does extraction too:** for destinations with a written parser, the proxy can log the full exchange into Bronze as a session transcript — one control point serving as both Egress Gate and capture point, but only as deep as the parsing goes.
- **Context injection back into a session (stretch, not core):** for MCP tools, injecting retrieved knowledge is just a normal tool call. For a browser chat UI, there's no API to add hidden context into someone else's hosted session — the only lever is the proxy rewriting the outbound request body before it leaves (prepending retrieved context to the user's message). Only works for parsed services; flagged as a possible future capability, not a hackathon build target.

### 4.3 Path 3 — manual fallback
For anything not covered by Path 1 or 2, the employee uses the Orgbrain dashboard directly to search or record something. Zero engineering, always available, zero automation. Worth stating explicitly rather than implying total coverage exists.

## 5. Interception mechanism — options and trade-offs for Path 2

This is the part that isn't confirmed. Four options were on the table:

### Option A: Browser extension
Intercepts text typed into web-based AI chat UIs (ChatGPT, Claude.ai, Gemini) before submission.
- ✅ Fastest to build, most visually demoable to judges (they watch it happen live in a browser).
- ❌ Doesn't cover desktop apps, IDE assistants, or direct API calls made by other programs.

### Option B: Local network proxy / agent — **current leaning**
The full Path 2 mechanism described in §4.2.
- ✅ Broadest coverage — catches browser, desktop apps, and CLI tools in one place, since everything funnels through the network layer.
- ✅ Matches the "software that sits on the device" framing most literally — one control point, not one per tool.
- ❌ TLS interception is real systems work, and if it goes wrong it can break other traffic on the device or trip security software.
- ❌ Setup/debugging time for TLS interception alone can eat a large share of a hackathon clock. This is the main reason it isn't confirmed yet — worth timeboxing a spike before committing the team to it.

### Option C: IDE / CLI plugin only
Hook into specific developer tools (VS Code extension, Claude Code / Copilot middleware) — relevant since 2 of the 4 teammates already have AI tools on their laptops.
- ✅ Narrowest surface, most reliable, fastest to integrate cleanly.
- ❌ Misses browser-based AI usage entirely, which is likely the most common shadow-AI path in practice.

### Option D: One real channel + shared policy engine
Build the actual interceptor for one channel only (e.g. Option A), but structure the detection/policy logic (destination classification, PII/confidential-data detection, block-vs-redact decision, the admin-floor/employee-ceiling config from §3) as a standalone service the interceptor calls. Any other channel built later (proxy, IDE plugin) calls the same service instead of duplicating logic.
- ✅ De-risks the demo — worst case, one channel works end-to-end and the policy engine itself is reusable and demoable on its own.
- This is worth doing **regardless of which interception mechanism wins** — don't let the choice above dictate whether the policy logic is reusable.

## 6. Recommendation for next step

> **RESOLVED (2026-08-09).** The spike below was not needed for the Anthropic API case. `gateway/` intercepts by BASE-URL REDIRECT (`ANTHROPIC_BASE_URL=http://localhost:8080 claude`) — one environment variable, no system proxy, no locally-trusted root CA, no per-OS work, and no cert-pinning failure mode. The honest scope of that: it covers the model API, not all of "AI". Option B below remains the answer for browser chat UIs, which the redirect cannot reach.

Before committing to Option B (local proxy/agent) for the full build: timebox a short spike (a couple hours) to get local TLS interception working end-to-end for a single test domain, on one OS only (whichever the demo machine runs). If it's straightforward, proceed with B, parsed for 2–3 known AI domains, with coarse allow/block for everything else. If it's fighting the environment, fall back to A (browser extension) as the demoable channel and keep B as a "how we'd extend this" slide rather than a build target. Don't attempt all three OSes or a comprehensive service list in a hackathon — that's real production DLP-vendor scope.

Build the policy engine (Option D's shared service, including the §3 config model) in parallel regardless of that spike's outcome — it isn't wasted work either way.

## 7. Open questions for whoever picks this up

1. Block vs. redact-and-continue — which content categories get which treatment? (Security/product call, not engineering.)
2. Is a full-traffic-intercepting proxy acceptable on team members' devices for the hackathon, or does that need IT/security sign-off first?
3. Where does the "known confidential document" corpus for fingerprinting come from, and who maintains it — in scope for the hackathon, or a "future work" slide?
4. ~~How does this interact with the Ingestion Gate — does Egress Gate activity ever get logged into the same audit trail?~~ — resolved by construction: there's only one on-device engine now (§1), so ingest-bound and AI-bound redactions both report into the same `redaction_audit_log` via the same `sensitivity_flags` metadata mechanism.
5. Concrete admin-floor list — which destinations/categories are actually mandatory on day one? Not yet defined, just the mechanism for defining them.
