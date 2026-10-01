"""Lightweight SQLite persistence for saved searches (new-job alerts).

There's no login system here - a visitor is identified by an anonymous id the UI stores in their
own browser (a cookie in Flask, a value stashed in Streamlit's session state), so their saved
searches are private to their own browser without needing an account.

The database file lives on the app's local disk. On most hosts (including Streamlit Community
Cloud) that disk is ephemeral - it survives while the app keeps running, but a redeploy or reboot
can reset it. That's an acceptable trade-off for saved searches; nothing here is data you can't
just recreate.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB_FILE = Path(__file__).with_name("job_scraper.db")
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with _lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS saved_searches (
            id TEXT PRIMARY KEY, visitor_id TEXT, query_json TEXT, webhook_url TEXT,
            created_at REAL, last_checked_at REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS seen_jobs (
            search_id TEXT, job_url TEXT, seen_at REAL, PRIMARY KEY (search_id, job_url))""")


def new_visitor_id() -> str:
    return uuid.uuid4().hex


# --- saved searches / alerts -------------------------------------------------------------------

def add_saved_search(visitor_id: str, query: dict, webhook_url: str = "") -> str:
    sid = uuid.uuid4().hex
    with _lock, _conn() as c:
        c.execute("INSERT INTO saved_searches VALUES (?,?,?,?,?,?)",
                  (sid, visitor_id, json.dumps(query), webhook_url, time.time(), 0))
    return sid


def list_saved_searches(visitor_id: str | None = None) -> list[dict]:
    with _lock, _conn() as c:
        if visitor_id:
            rows = c.execute("SELECT * FROM saved_searches WHERE visitor_id=?", (visitor_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM saved_searches").fetchall()
    out = []
    for id_, vid, qjson, webhook, created, checked in rows:
        out.append({"id": id_, "visitor_id": vid, "query": json.loads(qjson), "webhook_url": webhook,
                   "created_at": created, "last_checked_at": checked})
    return out


def remove_saved_search(sid: str) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM saved_searches WHERE id=?", (sid,))
        c.execute("DELETE FROM seen_jobs WHERE search_id=?", (sid,))


def mark_checked(sid: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE saved_searches SET last_checked_at=? WHERE id=?", (time.time(), sid))


def filter_new(sid: str, job_urls: list[str]) -> list[str]:
    """The subset of job_urls not seen before for this saved search. Records ALL given urls as seen
    (including ones already known), so the next check only reports genuinely new postings."""
    urls = [u for u in job_urls if u]
    with _lock, _conn() as c:
        seen = {r[0] for r in c.execute("SELECT job_url FROM seen_jobs WHERE search_id=?", (sid,))}
        c.executemany("INSERT OR IGNORE INTO seen_jobs VALUES (?,?,?)",
                      [(sid, u, time.time()) for u in urls])
    return [u for u in urls if u not in seen]


init()
