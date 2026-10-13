"""Background alerting for saved searches: periodically re-runs each one, finds jobs it hasn't seen
before, and POSTs them to that search's webhook URL. A generic webhook (not email) so it needs no
new API key or SMTP setup - it works as-is with a Slack "Incoming Webhook" or a Discord channel
webhook, or any URL of your own that accepts a JSON POST.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse

import requests

import store
from job_scraper import search_jobs

CHECK_EVERY = 30 * 60  # re-check every 30 min


def validate_webhook(url: str) -> str | None:
    """Returns None if `url` is safe to POST to, else a short reason it was rejected. The URL is
    user-supplied and fetched server-side, so it must be https and every address its host resolves
    to must be public - otherwise a visitor could aim the server at localhost, a cloud metadata
    endpoint or an internal service (SSRF)."""
    try:
        u = urlparse(url)
        host = u.hostname
        port = u.port or 443
    except ValueError:
        return "not a valid URL"
    if u.scheme != "https" or not host:
        return "must be an https:// URL"
    if u.username or u.password:
        return "must not contain credentials"
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)}
    except OSError:
        return "host could not be resolved"
    for a in addrs:
        if not ipaddress.ip_address(a.split("%")[0]).is_global:
            return "must point to a public host"
    return None


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
    # re-checked at send time (not just when saved) in case DNS now points somewhere internal, and
    # rows saved before validation existed; redirects are off so a public URL can't bounce inward
    bad = validate_webhook(saved["webhook_url"])
    if bad:
        print(f"[alerts] skipping webhook for saved search {saved['id']}: {bad}", flush=True)
        return
    try:
        # sent as both "text" (Slack-style) and "content" (Discord-style) so either works unmodified
        requests.post(saved["webhook_url"], json={"text": text, "content": text}, timeout=10,
                      allow_redirects=False)
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
