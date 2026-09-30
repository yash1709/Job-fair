"""Background alerting for saved searches: periodically re-runs each one, finds jobs it hasn't seen
before, and POSTs them to that search's webhook URL. A generic webhook (not email) so it needs no
new API key or SMTP setup - it works as-is with a Slack "Incoming Webhook" or a Discord channel
webhook, or any URL of your own that accepts a JSON POST.
"""
from __future__ import annotations

import threading
import time

import requests

import store
from job_scraper import search_jobs

CHECK_EVERY = 30 * 60  # re-check every 30 min


def _run_one(saved: dict) -> None:
    q = saved["query"]
    try:
        jobs = search_jobs(q.get("role", ""), q.get("experience") or None, q.get("location"),
                           q.get("sources"), country_code=q.get("country_code") or None,
                           date_restrict=q.get("date") or None, strict=q.get("strict", False),
                           min_salary=q.get("min_salary") or None, max_salary=q.get("max_salary") or None,
                           job_types=q.get("job_type") or [])
    except Exception as e:
        print(f"[alerts] saved search {saved['id']} failed: {e}", flush=True)
        return
    new_urls = set(store.filter_new(saved["id"], [j.url for j in jobs]))
    store.mark_checked(saved["id"])
    if not new_urls or not saved["webhook_url"]:
        return
    new_jobs = [j for j in jobs if j.url in new_urls]
    lines = [f"- {j.title} at {j.company} ({j.location}) - {j.url}" for j in new_jobs[:20]]
    text = f"{len(new_jobs)} new job(s) for \"{q.get('role', '')}\":\n" + "\n".join(lines)
    try:
        # sent as both "text" (Slack-style) and "content" (Discord-style) so either works unmodified
        requests.post(saved["webhook_url"], json={"text": text, "content": text}, timeout=10)
    except Exception as e:
        print(f"[alerts] webhook failed for saved search {saved['id']}: {e}", flush=True)


def check_all_now() -> int:
    """Runs every saved search once, immediately. Returns how many were checked. Used by the manual
    "check now" action in the UI, separate from the background loop's own schedule."""
    saved = store.list_saved_searches()
    for s in saved:
        _run_one(s)
    return len(saved)


def start() -> None:
    """Starts the periodic background loop. Safe to call multiple times from a caller that already
    guards against duplicate starts (e.g. st.cache_resource); this function itself doesn't guard."""
    def loop():
        while True:
            time.sleep(CHECK_EVERY)
            try:
                check_all_now()
            except Exception as e:
                print(f"[alerts] loop error: {e}", flush=True)

    threading.Thread(target=loop, daemon=True).start()
