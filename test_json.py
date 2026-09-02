from gateway.policies import write
from gateway.protocol.normalized import NormalizedResponse
text = """
This session identified three critical race conditions in the ESDS gateway's flows module that could cause awareness cooldown bypasses, log corruption, and lost pending writes under concurrent load. The _awareness_last dict and pending file operations lack synchronization, and the logging function opens files unsafely. These issues are latent in production but will surface under realistic multi-request concurrency.

{
  "content": "Identified three critical race conditions in gateway/flows.py: (1) awareness cooldown dict read-check-write without synchronization bypasses rate limiting under concurrent requests, (2) logging appends to a shared file without locking causing corruption, (3) pending state mutations lack locking, enabling concurrent draft approvals to lose updates and violate bus idempotency guarantees. These expose the system to duplicate expensive operations, lost writes, and inconsistent state.",
  "knowledge": {
    "title": "Concurrency vulnerabilities in ESDS gateway flows",
    "summary": "Three unprotected concurrent mutations in flows.py: awareness cooldown dict, debug log file, and pending draft state. All can race under multi-request load, causing cooldown bypasses, corrupted logs, and lost writes to the bus.",
    "outcome": "issue_found",
    "key_points": [
      "Awareness cooldown (_awareness_last dict) checked and written without lock — concurrent requests bypass rate limiting",
      "Log writes to /tmp/dp_debug.log unsafely — multiple appends can interleave",
      "Pending state (load/save/set_status) has no explicit locking — concurrent approvals of same draft race on disk"
    ],
    "next_steps": [
      "Add threading.Lock to _awareness_last dict operations",
      "Use atomic file operations (write-then-rename) or file locking for logs and pending state",
      "Consider per-session locks if pending file contention becomes an issue under load"
    ]
  }
}

To save this, type ESDS_APPROVE b107f9a1
"""
resp = NormalizedResponse(model="test", stop_reason="stop", usage={}, text=text)
print(write.find_draft(resp))
draft = write.find_draft(resp)
print(write.validate_draft(draft))
