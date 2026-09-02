#!/usr/bin/env python3
"""Read docs/usage_log.jsonl and write docs/MEASUREMENT-A.md.

Run after both arms of scripts/measure_cache.sh have completed.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "docs" / "usage_log.jsonl"
OUT_PATH = ROOT / "docs" / "MEASUREMENT-A.md"


def load_entries():
    if not LOG_PATH.exists():
        raise SystemExit(f"no usage log at {LOG_PATH} — run scripts/measure_cache.sh first")
    entries = []
    with open(LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def table_for_arm(entries, arm_label):
    rows = [e for e in entries if e.get("arm_label") == arm_label]
    if not rows:
        return f"_No entries found for arm `{arm_label}`._\n"
    lines = [
        "| turn | input_tokens | cache_read_input_tokens | cache_creation_input_tokens | output_tokens |",
        "|---|---|---|---|---|",
    ]
    for i, e in enumerate(rows, start=1):
        u = e.get("usage", {})
        lines.append(
            f"| {i} | {u.get('input_tokens', '—')} | {u.get('cache_read_input_tokens', '—')} "
            f"| {u.get('cache_creation_input_tokens', '—')} | {u.get('output_tokens', '—')} |"
        )
    return "\n".join(lines) + "\n"


def verdict(entries):
    arm_a = [e for e in entries if e.get("arm_label") == "arm_a_no_inject"]
    arm_b = [e for e in entries if e.get("arm_label") == "arm_b_inject"]
    if not arm_a or not arm_b:
        return "Insufficient data for a verdict — one or both arms produced no logged turns."

    def total_reads(rows):
        return sum(e.get("usage", {}).get("cache_read_input_tokens", 0) or 0 for e in rows)

    a_reads, b_reads = total_reads(arm_a), total_reads(arm_b)
    if a_reads == 0 and b_reads == 0:
        return (
            "**Reads never non-zero in either arm.** The measurement itself is broken, "
            "not necessarily the design — check that the conversation is long/large enough "
            "to clear the model's cacheable-prefix minimum, and that nothing dynamic "
            "(a timestamp, unsorted JSON) sits in the prefix. Fix before drawing conclusions."
        )
    if a_reads > 0 and b_reads == 0:
        return (
            "**Cache reads present without injection, absent with it.** This is the "
            "failure mode §2.2a warns about — injection is landing before a cache "
            "breakpoint (or replacing content earlier in the prefix rather than "
            "appending after it). Confirm `inject()` only appends to the tail of "
            "`messages[]` and never touches top-level `system`."
        )
    ratio = b_reads / a_reads if a_reads else float("inf")
    if ratio < 0.5:
        return (
            f"**Reads degrade substantially with injection on** (arm B is {ratio:.0%} of "
            "arm A's total cache reads). Likely the §2.2a 20-block lookback — check "
            "WIRE-FINDINGS.md for the cache_control breakpoint count found on this "
            "machine before assuming the fallback fix is available."
        )
    return (
        f"**Cache reads roughly unchanged** (arm B is {ratio:.0%} of arm A's total cache "
        "reads). Thesis holds for this conversation shape — safe to put this table on "
        "the day-3 slide."
    )


def main():
    entries = load_entries()
    out = ["# Measurement A — prompt cache cost of injection\n"]
    out.append(
        "Both arms ran through the gateway (arm A with `DP_INJECT=0`, arm B with "
        "`DP_INJECT=1`) so the comparison is gateway-vs-gateway with injection as the "
        "only variable — not gateway-vs-no-gateway, which would compare across different "
        "cache namespaces (caches are scoped per credential) and be invalid. See "
        "docs/TEST-PLAN.md T4 for why.\n"
    )
    out.append("## Arm A — no injection (`arm_a_no_inject`)\n")
    out.append(table_for_arm(entries, "arm_a_no_inject"))
    out.append("\n## Arm B — with injection (`arm_b_inject`)\n")
    out.append(table_for_arm(entries, "arm_b_inject"))
    out.append("\n## Verdict\n")
    out.append(verdict(entries) + "\n")
    OUT_PATH.write_text("\n".join(out))
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
