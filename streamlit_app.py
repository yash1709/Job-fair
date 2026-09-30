"""Streamlit Cloud entry point for the job scraper.

The Flask app (app.py) can't run on Streamlit Community Cloud, which only knows how to execute
Streamlit scripts (`streamlit run <file>`), not a Flask app that starts its own server. This file
is the equivalent UI built with Streamlit widgets, on top of the same job_scraper.py / careers.py
logic the Flask app uses - including bookmarks/hide, saved-search alerts, pagination, and a status
view, mirroring app.py's feature set within Streamlit's interaction model.

Local run:  streamlit run streamlit_app.py
Deploy:     point Streamlit Community Cloud's "Main file path" at this file.
Secrets:    set GOOGLE_API_KEY / GOOGLE_CSE_ID / ADZUNA_APP_ID / ADZUNA_APP_KEY / ADZUNA_COUNTRY /
            JOOBLE_API_KEY / JOBVETTA_API_KEY in the app's Settings -> Secrets (TOML), e.g.:
                GOOGLE_API_KEY = "..."
            They're copied into the environment below, since job_scraper.py reads them with os.getenv.
"""

import io
import math
import os
import threading
import time
import urllib.parse
from datetime import datetime

import streamlit as st

# Secrets set in Streamlit Cloud's "Secrets" panel arrive via st.secrets, not a .env file (.env is
# gitignored and never deployed). Copy anything relevant into the environment before it's read.
for _key in ("GOOGLE_API_KEY", "GOOGLE_CSE_ID", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "ADZUNA_COUNTRY",
            "JOOBLE_API_KEY", "JOBVETTA_API_KEY"):
    try:
        if _key in st.secrets and not os.environ.get(_key):
            os.environ[_key] = str(st.secrets[_key])
    except Exception:
        break  # no secrets.toml at all (e.g. local run without one) - fine, sources without a key just skip

import alerts
import careers
import quota
import stats
import store
from job_scraper import (DATE_WINDOWS, JOB_TYPES, LEVELS, SORTS, SOURCES, fmt_exp, fmt_posted, fmt_salary,
                         fmt_type, is_india, search_jobs, slugify, sort_jobs, write_csv)

st.set_page_config(page_title="Job Scraper", page_icon="🔍", layout="wide")

CACHE_TTL = 15 * 60  # matches the Flask app's search-result cache lifetime
PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Background careers-page loading and alert checking, so a visitor never blocks
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def start_background_refresh():
    """Runs once per deployed app instance (st.cache_resource is a process-wide singleton, shared
    across every visitor's session) - not once per page view. Mirrors app.py's keep_careers_warm()."""
    def loop():
        while True:
            t = time.time()
            for key, (_, jobs) in list(careers._board_cache.items()):  # expire, but keep as fallback
                careers._board_cache[key] = (0, jobs)
            jobs = careers.fetch_all()
            print(f"[careers] loaded {len(jobs)} jobs from {len(careers.load_companies())} companies "
                  f"in {time.time() - t:.0f}s", flush=True)
            time.sleep(60 * 60)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


@st.cache_resource(show_spinner=False)
def start_alerts():
    alerts.start()
    return True


start_background_refresh()  # body only ever runs once per app process, however many visitors call it
start_alerts()

# --- anonymous per-browser-session visitor id, for private bookmarks/saved searches ---------------
if "visitor_id" not in st.session_state:
    st.session_state["visitor_id"] = store.new_visitor_id()
VID = st.session_state["visitor_id"]

# --- one-shot row-action links (?job_action=bookmarked&job_url=...&on=1), Flask-style ------------
qp = st.query_params
if "job_action" in qp:
    job_url, action, on = qp.get("job_url", ""), qp.get("job_action", ""), qp.get("on", "1") == "1"
    if job_url and action in ("bookmarked", "applied", "hidden"):
        store.set_action(VID, job_url, action, on=on)
    st.query_params.clear()
    st.rerun()


def careers_loading(sources: list[str]) -> bool:
    status = careers.status()
    return "careers" in sources and (status["loading"] or status["cached"] == 0)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def cached_search(role, experience, location, sources, country_code, date, strict,
                  min_salary, max_salary, job_types):
    return search_jobs(role, experience or None, location, list(sources), country_code=country_code or None,
                       date_restrict=date or None, strict=strict, min_salary=min_salary or None,
                       max_salary=max_salary or None, job_types=list(job_types))


def run_search(role, experience, location, sources, country_code, date, strict,
               min_salary, max_salary, job_types):
    if careers_loading(sources):
        # Don't cache a partial result (careers pages still loading) - caching it would hide the
        # rest of those jobs from this same search for the next 15 minutes.
        return search_jobs(role, experience or None, location, sources, country_code=country_code or None,
                           date_restrict=date or None, strict=strict, min_salary=min_salary or None,
                           max_salary=max_salary or None, job_types=job_types)
    return cached_search(role, experience, location, tuple(sources), country_code, date, strict,
                         min_salary, max_salary, tuple(job_types))


def action_link(job_url: str, action: str, on: bool) -> str:
    return f"?job_action={action}&job_url={urllib.parse.quote(job_url, safe='')}&on={'1' if on else '0'}"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.markdown("""<style>
 .tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:#eef1f5;color:#444;margin-right:4px}
 .tag.remote{background:#e6f4ea;color:#137333} .tag.internship{background:#fef7e0;color:#b06000}
 .jobtable{width:100%;border-collapse:collapse}
 .jobtable th{background:#fafafa;text-align:left;padding:8px 10px;font-size:13px;border-bottom:1px solid #ddd}
 .jobtable td{padding:8px 10px;border-bottom:1px solid #eee;font-size:14px;vertical-align:top}
 .jobtable a{color:#1a73e8;text-decoration:none} .muted{color:#777;font-size:12px}
 .jobtable tr.hidden-row{opacity:.5}
 .rowact a{margin-right:6px;text-decoration:none}
</style>""", unsafe_allow_html=True)

st.title("🔍 Job Scraper")
st.caption("Google Custom Search + free job APIs, worldwide with India-focused sources")

tab_search, tab_saved, tab_status = st.tabs(["🔍 Search", "🔔 Saved searches", "⚙️ Status"])

with tab_search:
    with st.form("search"):
        c1, c2, c3 = st.columns(3)
        role = c1.text_input("Role", placeholder="python developer")
        experience = c2.selectbox("Experience", [""] + ["0-1", "0-2", "2-4", "3-5", "5+", "8+"] + list(LEVELS),
                                  format_func=lambda v: v or "Any")
        location = c3.text_input("Location(s)", placeholder="india, berlin, remote")

        c4, c5, c6 = st.columns(3)
        min_salary = c4.text_input("Min salary / yr", placeholder="12 LPA / 80k",
                                   help="Rupees for Indian locations, US dollars otherwise")
        max_salary = c5.text_input("Max salary / yr", placeholder="any")
        country_code = c6.text_input("Google country (gl)", placeholder="in / us / de")

        c7, c8 = st.columns(2)
        job_type = c7.multiselect("Job type", JOB_TYPES, format_func=str.capitalize,
                                  help="Full-time / Internship are alternatives; Remote narrows either")
        date = c8.selectbox("Posted within", [""] + list(DATE_WINDOWS), format_func=lambda v: DATE_WINDOWS.get(v, "Any time"))

        sources = st.multiselect("Sources", list(SOURCES), default=list(SOURCES))
        cchk1, cchk2 = st.columns(2)
        strict = cchk1.checkbox("Strict experience (drop jobs with no detectable experience info)")
        show_hidden = cchk2.checkbox("Show hidden jobs")
        submitted = st.form_submit_button("Search", type="primary")

    if submitted:
        if not role.strip():
            st.error("Role is required.")
        else:
            try:
                with st.spinner("Searching…"):
                    jobs = run_search(role, experience, location, sources, country_code, date, strict,
                                     min_salary, max_salary, job_type)
                st.session_state["results"] = jobs
                st.session_state["result_meta"] = {"role": role, "experience": experience, "location": location,
                                                   "sources": sources, "country_code": country_code, "date": date,
                                                   "strict": strict, "min_salary": min_salary,
                                                   "max_salary": max_salary, "job_type": job_type}
                st.session_state["page"] = 1
            except ValueError as e:
                st.error(str(e))
                st.session_state.pop("results", None)

    if "results" in st.session_state:
        jobs = st.session_state["results"]
        meta = st.session_state["result_meta"]
        cur = "INR" if is_india(meta["location"].split(",")) else "USD"

        if careers_loading(meta["sources"]):
            status = careers.status()
            st.info(f"Company careers pages are still loading ({status['done']} of {status['total'] or '…'} "
                    "companies). These results only include the ones loaded so far — search again in a "
                    "minute for all of them.")

        my_actions = store.get_actions(VID)
        jobs = sort_jobs(jobs, st.session_state.get("sort", "relevance"), cur)
        visible_jobs = jobs if show_hidden else [j for j in jobs if "hidden" not in my_actions.get(j.url, ())]
        total = len(visible_jobs)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(1, st.session_state.get("page", 1)), pages)

        top = st.columns([3, 1, 1])
        hidden_n = len(jobs) - total
        top[0].write(f"**{total} jobs found**" + (f" ({hidden_n} hidden)" if hidden_n else ""))
        sort = top[1].selectbox("Sort", list(SORTS), format_func=lambda v: SORTS[v],
                                label_visibility="collapsed", key="sort",
                                on_change=lambda: st.session_state.update(page=1))

        if visible_jobs:
            buf = io.StringIO()
            write_csv(visible_jobs, buf)
            name = ("jobs-" + "-".join(filter(None, [slugify(meta["role"]), slugify(meta["location"])]))
                   + datetime.now().strftime("-%Y%m%d") + ".csv")
            top[2].download_button("⬇ CSV", "﻿" + buf.getvalue(), file_name=name, mime="text/csv")

            page_jobs = visible_jobs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
            rows = []
            for j in page_jobs:
                acts = my_actions.get(j.url, set())
                tags = "".join(f'<span class="tag {t.lower()}">{t}</span>' for t in fmt_type(j).split(" · ") if t != "-")
                bm, ap, hd = "🔖" if "bookmarked" in acts else "📑", "✅" if "applied" in acts else "⬜", \
                    "👁" if "hidden" in acts else "✕"
                row_actions = (
                    f'<a href="{action_link(j.url, "bookmarked", "bookmarked" not in acts)}" title="Bookmark">{bm}</a>'
                    f'<a href="{action_link(j.url, "applied", "applied" not in acts)}" title="Mark applied">{ap}</a>'
                    f'<a href="{action_link(j.url, "hidden", "hidden" not in acts)}" title="Hide/unhide">{hd}</a>')
                rows.append(
                    f'<tr class="{"hidden-row" if "hidden" in acts else ""}">'
                    f"<td><a href='{j.url}' target='_blank' rel='noopener'>{j.title}</a></td>"
                    f"<td>{j.company}</td><td>{j.location}</td><td>{fmt_exp(j)}</td>"
                    f"<td style='white-space:nowrap'>{fmt_salary(j, cur)}</td><td>{tags or '-'}</td>"
                    f"<td class='muted' title='{j.posted}'>{fmt_posted(j)}</td><td class='muted'>{j.source}</td>"
                    f"<td class='rowact'>{row_actions}</td></tr>"
                )
            table = ("<table class='jobtable'><tr><th>Title</th><th>Company</th><th>Location</th>"
                     "<th>Experience</th><th>Salary / yr</th><th>Type</th><th>Posted</th><th>Source</th>"
                     "<th>Actions</th></tr>" + "".join(rows) + "</table>")
            st.markdown(table, unsafe_allow_html=True)

            if pages > 1:
                pcol1, pcol2, pcol3 = st.columns([1, 2, 1])
                if pcol1.button("← Prev", disabled=page <= 1):
                    st.session_state["page"] = page - 1
                    st.rerun()
                pcol2.markdown(f"<p style='text-align:center'>Page {page} of {pages}</p>", unsafe_allow_html=True)
                if pcol3.button("Next →", disabled=page >= pages):
                    st.session_state["page"] = page + 1
                    st.rerun()

            with st.expander("🔔 Save this search & get alerted when it finds a new job"):
                webhook = st.text_input("Webhook URL (Slack/Discord incoming webhook - optional)",
                                        key="webhook_input")
                if st.button("Save search"):
                    store.add_saved_search(VID, meta, webhook.strip())
                    st.success("Saved — see the 'Saved searches' tab.")

with tab_saved:
    searches = store.list_saved_searches(VID)
    if not searches:
        st.caption("No saved searches yet. Run a search and use \"Save this search\" below the results.")
    for s in searches:
        with st.container(border=True):
            q = s["query"]
            st.markdown(f"**{q.get('role', '')}** — {q.get('location', '') or 'anywhere'}")
            checked = ("never" if not s["last_checked_at"]
                      else datetime.fromtimestamp(s["last_checked_at"]).strftime("%Y-%m-%d %H:%M"))
            st.caption(f"Webhook: {s['webhook_url'] or '(none - tracks new jobs but sends no notification)'}"
                      f"  ·  Last checked: {checked}")
            bc1, bc2 = st.columns(2)
            if bc1.button("Check now", key=f"check-{s['id']}"):
                with st.spinner("Checking…"):
                    alerts._run_one(s)
                st.rerun()
            if bc2.button("Delete", key=f"del-{s['id']}"):
                store.remove_saved_search(s["id"])
                st.rerun()

with tab_status:
    st.subheader("Careers pages")
    cst = careers.status()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Companies configured", len(careers.load_companies()))
    m2.metric("Boards cached", cst["cached"])
    m3.metric("Loading now", f"{cst['done']}/{cst['total']}" if cst["loading"] else "no")
    m4.metric("Failing recently", cst["failed_recently"])

    st.subheader("Daily API quotas")
    usage = quota.usage()
    if usage:
        st.table([{"source": src, "used": u["used"], "limit": u["limit"], "remaining": u["remaining"]}
                 for src, u in usage.items()])
    else:
        st.caption("No metered sources configured.")

    st.subheader("Source health (this run)")
    snap = stats.snapshot()
    if snap:
        # one row per source (not one column per source) - keeps every column a single, consistent
        # type throughout, which a mixed dict-of-dicts orientation doesn't (that mixed ints and the
        # occasional long error string within the same column and broke Arrow serialization).
        st.table([{"source": src, "last_count": s["last_count"], "total_calls": s["total_calls"],
                  "total_errors": s["total_errors"], "last_error": s["last_error"] or ""}
                 for src, s in sorted(snap.items())])
    else:
        st.caption("No searches run yet this session.")
