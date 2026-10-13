"""Daily (and monthly) call-quota tracking for metered free APIs (Jobvetta, Google CSE, Adzuna, Jooble), plus a
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

# Caps that reset monthly (UTC calendar month). SerpApi's free plan is 100 searches/MONTH, so a daily
# counter can't express it: it either wastes most of the month or lets one busy day burn it all.
MONTHLY_LIMITS: dict[str, int] = {
    "serpapi": int(os.getenv("SERPAPI_MONTHLY_LIMIT", "100")),
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load() -> dict:
    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    if data.get("date") != _today():
        data["date"], data["counts"] = _today(), {}
    if data.get("month") != _month():
        data["month"], data["month_counts"] = _month(), {}
    data.setdefault("counts", {})
    data.setdefault("month_counts", {})
    return data


def _save(data: dict) -> None:
    try:
        QUOTA_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def allow(source: str, cost: int = 1) -> bool:
    """True (and records the call) if quota remains, or the source isn't metered. False if the
    day's quota is used up - the caller should skip the call entirely."""
    daily, monthly = DAILY_LIMITS.get(source), MONTHLY_LIMITS.get(source)
    if not daily and not monthly:  # 0 or missing = unmetered
        return True
    with _lock:
        data = _load()
        used, month_used = data["counts"].get(source, 0), data["month_counts"].get(source, 0)
        if (daily and used + cost > daily) or (monthly and month_used + cost > monthly):
            return False
        data["counts"][source] = used + cost
        data["month_counts"][source] = month_used + cost
        _save(data)
    return True


def usage() -> dict[str, dict]:
    """{"jobvetta": {"used": 3, "limit": 50, "remaining": 47}, ...} - for a status view."""
    with _lock:
        data = _load()
    out = {src: {"used": data["counts"].get(src, 0), "limit": limit,
                 "remaining": max(0, limit - data["counts"].get(src, 0))}
           for src, limit in DAILY_LIMITS.items() if limit}
    for src, limit in MONTHLY_LIMITS.items():
        if limit:
            used = data["month_counts"].get(src, 0)
            out[src] = {"used": used, "limit": limit, "remaining": max(0, limit - used), "period": "month"}
    return out


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


# --- per-client rate limit ---------------------------------------------------------------------
# The app-wide limit above only protects the metered quotas; this one stops a single client from
# hammering the public JSON/CSV endpoints (each call fans out to every source).

MAX_REQUESTS_PER_CLIENT_PER_MINUTE = int(os.getenv("MAX_REQUESTS_PER_CLIENT_PER_MINUTE", "30"))
_client_times: dict[str, list[float]] = {}


def client_allowed(client: str) -> bool:
    now = time.time()
    with _lock:
        if len(_client_times) > 5000:  # don't grow forever under many distinct clients
            for k in [k for k, v in _client_times.items() if not v or now - v[-1] > 60]:
                del _client_times[k]
        times = [t for t in _client_times.get(client, []) if now - t <= 60]
        if len(times) >= MAX_REQUESTS_PER_CLIENT_PER_MINUTE:
            _client_times[client] = times
            return False
        times.append(now)
        _client_times[client] = times
    return True
