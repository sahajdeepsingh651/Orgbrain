# Data Passport — Problem & Topic Analysis

> For GitHub issue #2 ("Data Passport topic analysis") — that issue currently only has the raw problem statement. This doc breaks it down and states which parts we're actually solving.

## 1. Original problem statement

> **Data Passport: Carry Knowledge Forward**
>
> Your organization doesn't have a passport to move knowledge across teams, across tools, across time. Every insight stays where it was born; none of it travels. Your newest hire has no access to your most senior thinking. Your fastest-growing team has no memory of how the slowest-growing team solved the same problem three years ago. Two teams make contradictory decisions in the same month and neither finds out until the work collides. Nobody can see what anyone else's AI is working on right now, and no agent can pick up where another left off. And the one thing that does travel is the thing that shouldn't — PII, credentials and customer data, walking out through AI prompts nobody is watching.

## 2. Breaking it into sub-problems

The statement bundles several distinct problems:

| # | Sub-problem | In scope for hackathon? |
|---|---|---|
| A | Insights stay siloed — new hires and other teams can't find what a team already knows | **Yes** — knowledge capture & search |
| B | Teams re-solve problems other teams already solved (no institutional memory across time) | **Yes** — same mechanism as A (search surfaces past work) |
| C | Two teams make contradictory decisions and don't find out until collision | **No** — deferred, real feature but needs cross-team comparison logic beyond hackathon time |
| D | Nobody can see what anyone else's AI agent is working on | **Yes** — shared agent activity ledger |
| E | No agent can pick up where another left off | **Yes** — handoff mechanism, same underlying data as D |
| F | PII/credentials/customer data leak out through AI prompts unmonitored | **Yes** — one on-device checkpoint, two destinations: always-redact toward the shared knowledge base, destination-based policy toward external AI |

## 3. The angle we chose

Sub-problems A, B, D, E are all "knowledge that should move, doesn't." Sub-problem F is the inverse: "data that shouldn't move, does." Building both halves at once is what makes the demo land as *Data Passport* rather than as a generic search tool or a generic DLP tool — the pitch is literally: **we fixed both directions of the same border at once.**

C (contradiction detection) is a real, valid extension of the same theme but was deliberately deferred — seeing the full reasoning in `decisions-log.md` (2026-08-07, "Hackathon prototype scope narrowed to...").

## 4. Where each part is actually designed

| Sub-problem | Design doc |
|---|---|
| A, B — knowledge capture & search | `data-passport-architecture.md` (Bronze/Gate/Silver/Gold), `data-passport-schema.md` (what gets extracted from a session) |
| D, E — agent visibility & handoff | `data-passport-architecture.md` §4 (MCP tools: `announce_task`, `get_agent_activity`, `handoff`) |
| F — PII/confidential data control | `data-passport-architecture.md` § The Endpoint Checkpoint (ingest-bound redaction), `data-passport-security-egress.md` (AI-bound Egress Gate policy) |
| Competitive/SOTA grounding | `glean-research.md` |
| Full reasoning trail for every choice above | `decisions-log.md` |

## 5. Still open (tracked, not yet resolved)

These came out of issue #3's research topics and aren't fully closed yet:

1. **Connector integration with AI harness tools** — MCP is the current plan (`data-passport-architecture.md` §4), but issue #3 asked to look into alternatives too; no alternatives have been evaluated yet.
2. **Concrete PII/confidential-data regex rules** — `data-passport-security-egress.md` defines the *categories* (PII, credentials, financial/pricing data) but not the actual rule set. Needs: decide exactly what counts as confidential for this org, then write the rules.
3. Contradiction detection (sub-problem C) — explicitly out of scope, listed here so it isn't forgotten if time allows.
