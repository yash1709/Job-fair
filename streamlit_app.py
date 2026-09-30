"""Streamlit Cloud entry point for the job scraper.

The Flask app (app.py) can't run on Streamlit Community Cloud, which only knows how to execute
Streamlit scripts (`streamlit run <file>`), not a Flask app that starts its own server. This file
is the equivalent UI built with Streamlit widgets, on top of the same job_scraper.py / careers.py
logic the Flask app uses.

Local run:  streamlit run streamlit_app.py
Deploy:     point Streamlit Community Cloud's "Main file path" at this file.
Secrets:    set GOOGLE_API_KEY / GOOGLE_CSE_ID / ADZUNA_APP_ID / ADZUNA_APP_KEY / ADZUNA_COUNTRY /
            JOOBLE_API_KEY / JOBVETTA_API_KEY in the app's Settings -> Secrets (TOML), e.g.:
                GOOGLE_API_KEY = "..."
            They're copied into the environment below, since job_scraper.py reads them with os.getenv.
"""

import io
import os
import threading
import time
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

import careers
from job_scraper import (DATE_WINDOWS, JOB_TYPES, LEVELS, SORTS, SOURCES, fmt_exp, fmt_posted, fmt_salary,
                         fmt_type, is_india, search_jobs, slugify, sort_jobs, write_csv)

st.set_page_config(page_title="Job Scraper", page_icon="🔍", layout="wide")

CACHE_TTL = 15 * 60  # matches the Flask app's search-result cache lifetime


# ---------------------------------------------------------------------------
# Background careers-page loading, so a visitor never blocks on a ~10 min load
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


start_background_refresh()  # body only ever runs once per app process, however many visitors call it


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
</style>""", unsafe_allow_html=True)

st.title("🔍 Job Scraper")
st.caption("Google Custom Search + free job APIs, worldwide with India-focused sources")

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
    strict = st.checkbox("Strict experience (drop jobs with no detectable experience info)")
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
            st.session_state["result_meta"] = {"role": role, "location": location, "sources": sources}
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

    top = st.columns([3, 1, 1])
    top[0].write(f"**{len(jobs)} jobs found**")
    sort = top[1].selectbox("Sort", list(SORTS), format_func=lambda v: SORTS[v], label_visibility="collapsed")
    jobs = sort_jobs(jobs, sort, cur)

    if jobs:
        buf = io.StringIO()
        write_csv(jobs, buf)
        name = ("jobs-" + "-".join(filter(None, [slugify(meta["role"]), slugify(meta["location"])]))
               + datetime.now().strftime("-%Y%m%d") + ".csv")
        top[2].download_button("⬇ CSV", "﻿" + buf.getvalue(), file_name=name, mime="text/csv")

        rows = []
        for j in jobs:
            tags = "".join(f'<span class="tag {t.lower()}">{t}</span>' for t in fmt_type(j).split(" · ") if t != "-")
            rows.append(
                f"<tr><td><a href='{j.url}' target='_blank' rel='noopener'>{j.title}</a></td>"
                f"<td>{j.company}</td><td>{j.location}</td><td>{fmt_exp(j)}</td>"
                f"<td style='white-space:nowrap'>{fmt_salary(j, cur)}</td><td>{tags or '-'}</td>"
                f"<td class='muted' title='{j.posted}'>{fmt_posted(j)}</td><td class='muted'>{j.source}</td></tr>"
            )
        table = ("<table class='jobtable'><tr><th>Title</th><th>Company</th><th>Location</th>"
                 "<th>Experience</th><th>Salary / yr</th><th>Type</th><th>Posted</th><th>Source</th></tr>"
                 + "".join(rows) + "</table>")
        st.markdown(table, unsafe_allow_html=True)
