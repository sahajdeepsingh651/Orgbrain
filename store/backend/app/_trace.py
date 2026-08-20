"""Opt-in call tracer — DP_TRACE=1 to enable. Store-backend twin of gateway/_trace.py.

Duplicated rather than shared: the gateway and the store backend are two
separate processes with two separate Python import roots (this one is run
as `uvicorn app.main:app` from inside store/backend/), so there is no single
package path both could import from without adding sys.path plumbing neither
process otherwise needs. See gateway/_trace.py for the full docstring.

Env vars: same names as the gateway tracer, but the default log file is
/tmp/dp_trace_store.log so the two processes don't interleave into one file.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_ENABLED = os.environ.get("DP_TRACE") == "1"
_LOG_RETURNS = os.environ.get("DP_TRACE_RETURNS") == "1"
_LOG_PATH = Path(os.environ.get("DP_TRACE_LOG", "/tmp/dp_trace_store.log"))
_ONLY = [s for s in os.environ.get("DP_TRACE_FILTER", "").split(",") if s]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent  # store/backend/
_EXCLUDE_MARKERS = (
    f"{os.sep}.venv{os.sep}",
    f"{os.sep}site-packages{os.sep}",
    f"{os.sep}tests{os.sep}",
)

_depth: dict[int, int] = {}
_lock = threading.Lock()
_fh = None


def _relpath(filename: str) -> str | None:
    # Synthetic frames (<frozen importlib._bootstrap>, <string>, <stdin>, ...)
    # aren't real paths. Path.resolve() doesn't require existence, so one of
    # these resolves relative to cwd and — since cwd IS store/backend/ when
    # you launch uvicorn from there — silently passes the relative_to() check
    # below. is_file() is what actually excludes them.
    if filename.startswith("<"):
        return None
    if any(m in filename for m in _EXCLUDE_MARKERS):
        return None
    try:
        p = Path(filename).resolve()
        if not p.is_file():
            return None
        rel = str(p.relative_to(_PROJECT_ROOT))
    except (OSError, ValueError):
        return None
    if _ONLY and not any(o in rel for o in _ONLY):
        return None
    return rel


def _profiler(frame, event, arg):
    if event not in ("call", "return"):
        return
    rel = _relpath(frame.f_code.co_filename)
    if rel is None:
        return
    tid = threading.get_ident()
    with _lock:
        depth = _depth.get(tid, 0)
        if event == "call":
            indent = depth
            _depth[tid] = depth + 1
            arrow = "→"
        else:
            depth = max(depth - 1, 0)
            _depth[tid] = depth
            indent = depth
            arrow = "←"
            if not _LOG_RETURNS:
                return
        ts = time.strftime("%H:%M:%S")
        _fh.write(f"{ts}  {'  ' * indent}{arrow} {rel}:{frame.f_lineno} {frame.f_code.co_name}()\n")
        _fh.flush()


def install_tracer() -> None:
    global _fh
    if not _ENABLED:
        return
    _fh = open(_LOG_PATH, "a")
    _fh.write(f"\n--- trace started {time.strftime('%Y-%m-%d %H:%M:%S')} (pid {os.getpid()}) ---\n")
    _fh.flush()
    sys.setprofile(_profiler)
    threading.setprofile(_profiler)
    print(f"[TRACE] DP_TRACE=1 -> call trace being written to {_LOG_PATH}", file=sys.stderr, flush=True)
