"""G6 — the pending-approval store.

The single most important property of this module: **a draft that reaches
here has NOT been written to the Context Bus, and nothing in this module
can write it there.** Persistence to the bus happens only in the approval
path, only after a human types ESDS_APPROVE. Validation is not approval —
a draft can be perfectly schema-valid and DLP-clean and still must sit here
until a person says yes.

Keyed by (session_id, pending_id). session_id comes from the adapter (G1),
never from the model, so one session cannot approve another's draft.

Disk-backed under DP_PENDING_DIR (default /tmp/dp_pending) because the
gateway is a long-lived process that gets restarted mid-demo; an in-memory
dict loses the draft between the turn that made it and the turn that
approves it. One JSON file per pending draft, named by pending_id.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

PENDING_DIR = Path(os.environ.get("DP_PENDING_DIR", "/tmp/dp_pending"))

STATUS_PENDING = "pending_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_QUEUED = "queued_bus_unavailable"


def _dir() -> Path:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    return PENDING_DIR


def new_id() -> str:
    """Short, typeable, unambiguous. The human has to retype this in a
    terminal, so 8 hex chars — not a full UUID."""
    return uuid.uuid4().hex[:8]


def _path(pending_id: str) -> Path:
    # pending_id reaches us from a human-typed marker argument, so it is
    # untrusted input being used to build a path. Reject anything that is
    # not plain hex rather than sanitising it.
    if not pending_id or not all(c in "0123456789abcdef" for c in pending_id.lower()):
        raise ValueError(f"invalid pending id: {pending_id!r}")
    return _dir() / f"{pending_id}.json"


def idempotency_key(session_id: str, draft: dict) -> str:
    """sha256(session_id + canonical draft JSON).

    Canonical = sort_keys + tight separators, so the same draft always
    hashes the same regardless of key order. This is what makes a retry
    after a timeout return the original record instead of a duplicate
    passport (store S2).
    """
    canonical = json.dumps(draft, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{session_id}\n{canonical}".encode()).hexdigest()


import threading

_pending_lock = threading.Lock()

def save(*, pending_id: str, session_id: str, account_uuid: str | None,
         draft: dict, sensitivity_flags: dict, warnings: list[str] | None = None) -> dict:
    """Persist a validated, DLP-scanned draft as PENDING. Never sends."""
    record = {
        "pending_id": pending_id,
        "session_id": session_id,
        "account_uuid": account_uuid,
        "draft": draft,
        "sensitivity_flags": sensitivity_flags,
        "warnings": warnings or [],
        "status": STATUS_PENDING,
        "created_at": time.time(),
    }
    path = _path(pending_id)
    tmp_path = path.with_suffix(".tmp")
    
    with _pending_lock:
        tmp_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)

    return record


def load(pending_id: str, *, session_id: str | None = None) -> dict | None:
    """Fetch a pending draft. If `session_id` is given it MUST match —
    a draft belongs to the session that created it, so one session cannot
    approve another's. Returns None for both "no such id" and "not yours",
    deliberately collapsed so the caller cannot probe for other sessions'
    ids."""
    try:
        path = _path(pending_id)
    except ValueError:
        return None
        
    with _pending_lock:
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
            
    if session_id is not None and record.get("session_id") != session_id:
        return None
    return record


def set_status(pending_id: str, status: str, **extra) -> dict | None:
    with _pending_lock:
        try:
            path = _path(pending_id)
        except ValueError:
            return None
            
        if not path.exists():
            return None
            
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
            
        record["status"] = status
        record.update(extra)
        
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)

    return record


def delete(pending_id: str) -> bool:
    try:
        path = _path(pending_id)
    except ValueError:
        return False
    if path.exists():
        path.unlink()
        return True
    return False


def list_queued(session_id: str | None = None) -> list[dict]:
    """Approved writes the bus was unreachable for (G8). These carry a
    frozen `ingest_payload` — the exact body the human approved — so the
    drain retries what was approved, not a re-derivation of it."""
    return _list_with_status(STATUS_QUEUED, session_id)


def list_pending(session_id: str | None = None) -> list[dict]:
    return _list_with_status(STATUS_PENDING, session_id)


def _list_with_status(status: str, session_id: str | None) -> list[dict]:
    out = []
    for path in sorted(_dir().glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if record.get("status") != status:
            continue
        if session_id is not None and record.get("session_id") != session_id:
            continue
        out.append(record)
    return out
