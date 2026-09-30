"""Daily call-quota tracking for metered free APIs (Jobvetta, Google CSE, Adzuna, Jooble), plus a
simple app-wide search rate limit.

Once the app is publicly deployed, these quotas are a shared resource across every visitor, not just
the owner - a handful of people searching could exhaust Jobvetta's 50-calls/day limit in minutes and
leave it dead for everyone else. This module makes every metered source check its remaining quota
before calling out, and gives the app a way to throttle itself under heavy traffic.

Counts are persisted to a small JSON file next to this module, so they survive process restarts
within the same UTC day (important on hosts that restart the app periodically). On a read-only
filesystem the file write just fails silently and quotas reset every restart - not ideal, but safe.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

QUOTA_FILE = Path(__file__).with_name("quota_state.json")
_lock = threading.Lock()

# Known free-tier daily caps. Override via env if your plan differs; omit a source here (or set its
# env var to "0" to disable tracking, non-zero to cap) to leave it unmetered.
DAILY_LIMITS: dict[str, int] = {
    "jobvetta": int(os.getenv("JOBVETTA_DAILY_LIMIT", "50")),
    "google": int(os.getenv("GOOGLE_DAILY_LIMIT", "100")),
    "adzuna": int(os.getenv("ADZUNA_DAILY_LIMIT", "250")),
    "jooble": int(os.getenv("JOOBLE_DAILY_LIMIT", "500")),
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    if data.get("date") != _today():
        data = {"date": _today(), "counts": {}}
    data.setdefault("counts", {})
    return data


def _save(data: dict) -> None:
    try:
        QUOTA_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def allow(source: str, cost: int = 1) -> bool:
    """True (and records the call) if quota remains, or the source isn't metered. False if the
    day's quota is used up - the caller should skip the call entirely."""
    limit = DAILY_LIMITS.get(source)
    if not limit:  # 0 or missing = unmetered
        return True
    with _lock:
        data = _load()
        used = data["counts"].get(source, 0)
        if used + cost > limit:
            return False
        data["counts"][source] = used + cost
        _save(data)
    return True


def usage() -> dict[str, dict]:
    """{"jobvetta": {"used": 3, "limit": 50, "remaining": 47}, ...} - for a status view."""
    with _lock:
        data = _load()
    return {src: {"used": data["counts"].get(src, 0), "limit": limit,
                  "remaining": max(0, limit - data["counts"].get(src, 0))}
            for src, limit in DAILY_LIMITS.items() if limit}


# --- app-wide search rate limit --------------------------------------------------------------
# Protects the metered sources from being hammered by a burst of visitors, independent of the
# per-day cap above. Deliberately process-global (not per-visitor): the goal is "don't let the
# whole app blow through its daily quota in the first five minutes", not fairness between users.

MAX_SEARCHES_PER_MINUTE = int(os.getenv("MAX_SEARCHES_PER_MINUTE", "20"))
_search_times: list[float] = []


def metered_sources_allowed() -> bool:
    """False if the app has done too many searches in the last minute - the caller should skip
    metered sources (Google/Adzuna/Jooble/Jobvetta) for this search, but keyless sources are fine
    (they have no shared daily quota to protect)."""
    now = time.time()
    with _lock:
        while _search_times and now - _search_times[0] > 60:
            _search_times.pop(0)
        if len(_search_times) >= MAX_SEARCHES_PER_MINUTE:
            return False
        _search_times.append(now)
    return True
