"""Opt-in call tracer — DP_TRACE=1 to enable.

Logs every function call inside this project's own code (not the venv, not
site-packages, not tests) to a file, in call order, indented by call depth —
so a live request shows you exactly which file/function ran, in what order,
with zero changes to the pipeline's own files.

Off by default: sys.setprofile fires on every call in the whole process
(FastAPI, Starlette, httpx included, even though only project files get
logged) and that overhead has no reason to be paid outside a tracing run.

Env vars:
    DP_TRACE          "1" to enable. Unset/anything else: this module is a no-op.
    DP_TRACE_LOG      Log file path (default /tmp/dp_trace_gateway.log).
    DP_TRACE_RETURNS  "1" to also log when a function returns (default: calls only).
    DP_TRACE_FILTER   Comma-separated substrings; if set, only paths containing
                       one of them are logged (e.g. "gateway/policies" to watch
                       only the policy layer).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_ENABLED = os.environ.get("DP_TRACE") == "1"
_LOG_RETURNS = os.environ.get("DP_TRACE_RETURNS") == "1"
_LOG_PATH = Path(os.environ.get("DP_TRACE_LOG", "/tmp/dp_trace_gateway.log"))
_ONLY = [s for s in os.environ.get("DP_TRACE_FILTER", "").split(",") if s]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
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
    # these resolves relative to cwd and — since cwd IS the project root when
    # you launch uvicorn from here — silently passes the relative_to() check
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
