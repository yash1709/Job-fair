"""In-memory operational stats: per-source fetch counts/errors and timestamps, for a status view.
Deliberately not persisted - it's meant to answer "is anything broken right now", which only needs
the current process's lifetime, not history across restarts.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_stats: dict[str, dict] = {}


def record(source: str, count: int | None, error: str | None = None) -> None:
    with _lock:
        s = _stats.setdefault(source, {"last_count": None, "last_error": None, "last_at": None,
                                       "total_calls": 0, "total_errors": 0})
        s["last_at"] = time.time()
        s["total_calls"] += 1
        if error:
            s["last_error"] = error
            s["total_errors"] += 1
        else:
            s["last_count"] = count
            s["last_error"] = None


def snapshot() -> dict[str, dict]:
    with _lock:
        return {k: dict(v) for k, v in _stats.items()}
