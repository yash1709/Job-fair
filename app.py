"""Web UI for the job scraper.  Run:  python app.py  ->  http://127.0.0.1:5000"""

import io
import math
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime

from flask import Flask, Response, jsonify, redirect, render_template_string, request, url_for

import alerts
import careers
import quota
import stats
import store
from job_scraper import (DATE_WINDOWS, JOB_TYPES, LEVELS, SORTS, SOURCES, fmt_exp, fmt_posted, fmt_salary,
                         fmt_type, is_india, search_jobs, slugify, sort_jobs, write_csv)

app = Flask(__name__)

# Last results per query, so "Download CSV" / pagination / re-sorting doesn't re-scrape every source.
CACHE: dict[str, tuple[float, list]] = {}
CACHE_TTL = 15 * 60
CACHE_MAX = 20  # searches kept for re-sorting / CSV download
PAGE_SIZE = 50

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Scraper</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1c1e21}
 header{background:#1a73e8;color:#fff;padding:16px 24px;display:flex;justify-content:space-between;align-items:center}
 header nav a{color:#e8f0fe;text-decoration:none;font-size:13px;margin-left:16px}
 header nav a:hover{color:#fff;text-decoration:underline}
 form{display:flex;flex-wrap:wrap;gap:12px;padding:16px 24px;background:#fff;border-bottom:1px solid #ddd}
 label{display:flex;flex-direction:column;font-size:12px;gap:4px;color:#555}
 input,select{padding:8px;border:1px solid #ccc;border-radius:6px;font-size:14px;min-width:160px}
 button{align-self:end;padding:9px 20px;background:#1a73e8;color:#fff;border:0;border-radius:6px;cursor:pointer}
 .src{display:flex;gap:10px;align-items:center;font-size:13px}
 main{padding:16px 24px} table{width:100%;border-collapse:collapse;background:#fff}
 th,td{padding:8px 10px;border-bottom:1px solid #eee;text-align:left;font-size:14px;vertical-align:top}
 th{background:#fafafa}
 .bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
 .btn{padding:8px 16px;background:#188038;color:#fff;border-radius:6px;font-size:14px;border:0;cursor:pointer}
 .btn:hover{background:#137333} .btn.secondary{background:#5f6368} .btn.secondary:hover{background:#444}
 .note{background:#fef7e0;border:1px solid #f9e0a0;color:#7a4b00;padding:8px 12px;border-radius:6px;font-size:14px} .err{color:#c5221f;font-weight:600}
 .salary,.type{white-space:nowrap}
 .types{display:flex;gap:6px;align-items:end;padding-bottom:2px}
 .chip{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border:1px solid #ccc;border-radius:16px;
   font-size:13px;color:#333;cursor:pointer;flex-direction:row}
 .chip:has(input:checked){background:#e8f0fe;border-color:#1a73e8;color:#1a73e8}
 .chip input{min-width:0;margin:0}
 .tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:#eef1f5;color:#444}
 .tag.remote{background:#e6f4ea;color:#137333} .tag.internship{background:#fef7e0;color:#b06000}
 .group{display:flex;gap:6px;align-items:end} a{color:#1a73e8;text-decoration:none} .muted{color:#777;font-size:12px}
 .actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap} .actions select{min-width:0}
 th a{color:inherit} th a:hover{color:#1a73e8}
 .rowact a{margin-right:8px;font-size:13px;text-decoration:none;white-space:nowrap}
 .rowact a.on{font-weight:700}
 .save-form{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .save-form input{min-width:220px}
 .pager{display:flex;gap:10px;align-items:center;justify-content:center;padding:16px 0}
 .pager a,.pager span{padding:6px 12px;border-radius:6px;font-size:14px}
 .pager a{background:#fff;border:1px solid #ccc}
 tr.hidden-row{opacity:.5}
</style></head><body>
<header><b>Job Scraper</b> — Google Custom Search + free job APIs
 <nav><a href="{{ url_for('saved_searches') }}">🔔 Saved searches</a><a href="{{ url_for('status_page') }}">⚙ Status</a></nav>
</header>
<form method="get" id="search">
 <label>Role<input name="role" value="{{ q.role }}" placeholder="python developer" required></label>
 <label>Experience
  <select name="experience">
   <option value="">Any</option>
   {% for e in ["0-1","0-2","2-4","3-5","5+","8+"] + levels %}
   <option {{ 'selected' if q.experience==e }}>{{ e }}</option>{% endfor %}
  </select></label>
 <label>Location(s)<input name="location" value="{{ q.location }}" placeholder="india, berlin, remote"></label>
 <div class="group">
  <label>Min salary / yr<input name="min_salary" value="{{ q.min_salary }}" placeholder="12 LPA / 80k" title="Rupees for Indian locations, US dollars otherwise" style="min-width:110px;width:110px"></label>
  <label>Max salary / yr<input name="max_salary" value="{{ q.max_salary }}" placeholder="any" style="min-width:110px;width:110px"></label>
 </div>
 <div class="types" title="Full-time / Internship are alternatives; Remote narrows either">
  {% for t in job_types %}<label class="chip"><input type="checkbox" name="job_type" value="{{ t }}"
   {{ 'checked' if t in q.job_type }}>{{ t|capitalize }}</label>{% endfor %}
 </div>
 <label>Posted within
  <select name="date"><option value="">Any time</option>{% for v, t in date_windows.items() %}
   <option value="{{v}}" {{ 'selected' if q.date==v }}>{{t}}</option>{% endfor %}</select></label>
 <div class="src">
  <label style="flex-direction:row;font-weight:600"><input type="checkbox" id="all-sources" style="min-width:0">Select all</label>
  {% for s in sources %}
  <label style="flex-direction:row"><input type="checkbox" name="sources" value="{{s}}" class="source" style="min-width:0"
   {{ 'checked' if s in q.sources }}>{{s}}</label>{% endfor %}
  <label style="flex-direction:row"><input type="checkbox" name="strict" value="1" style="min-width:0"
   {{ 'checked' if q.strict }}>strict experience</label>
  <label style="flex-direction:row"><input type="checkbox" name="show_hidden" value="1" style="min-width:0"
   {{ 'checked' if q.show_hidden }}>show hidden</label></div>
 <button>Search</button>
</form>
<main>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if careers_note %}<p class="note">{{ careers_note }}</p>{% endif %}
{% if jobs is not none %}
 <div class="bar"><p>{{ total }} jobs found{% if total != jobs|length %} ({{ jobs|length }} hidden){% endif %}</p>
  {% if total %}<div class="actions">
   <label style="flex-direction:row;align-items:center;gap:6px">Sort
    <select name="sort" form="search" onchange="this.form.submit()">{% for v, t in sorts.items() %}
     <option value="{{ v }}" {{ 'selected' if sort==v }}>{{ t }}</option>{% endfor %}</select></label>
   <a class="btn" href="/download.csv?{{ request.query_string.decode() }}">⬇ Download CSV</a>
   <form class="save-form" method="post" action="{{ url_for('save_search') }}">
    {% for k in ['role','experience','location','date','min_salary','max_salary'] %}
    <input type="hidden" name="{{ k }}" value="{{ q[k] }}">{% endfor %}
    {% for s in q.sources %}<input type="hidden" name="sources" value="{{ s }}">{% endfor %}
    {% for t in q.job_type %}<input type="hidden" name="job_type" value="{{ t }}">{% endfor %}
    <input name="webhook_url" placeholder="Slack/Discord webhook URL (optional)">
    <button class="btn secondary" type="submit" title="Get alerted by webhook when this search finds a new job">🔔 Save &amp; alert me</button>
   </form>
  </div>{% endif %}</div>
 <table><tr><th>Title</th><th>Company</th><th>Location</th><th>Experience</th><th><a href="{{ sort_url('salary_asc' if sort=='salary_desc' else 'salary_desc') }}"
   title="Sort by salary">Salary / yr {{ '▼' if sort=='salary_desc' else '▲' if sort=='salary_asc' else '↕' }}</a></th><th>Type</th><th>Posted</th><th>Source</th><th>Actions</th></tr>
 {% for j in jobs %}<tr {{ 'class=hidden-row' if 'hidden' in my_actions.get(j.url, []) }}>
  <td><a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a></td>
  <td>{{ j.company }}</td><td>{{ j.location }}</td><td>{{ fmt_exp(j) }}</td>
  <td class="salary">{{ fmt_salary(j, cur) }}</td>
  <td class="type">{% for t in fmt_type(j).split(" · ") if t != "-" %}<span class="tag {{ t|lower }}">{{ t }}</span> {% endfor %}</td>
  <td class="muted" title="{{ j.posted }}">{{ fmt_posted(j) }}</td><td class="muted">{{ j.source }}</td>
  <td class="rowact">{% set acts = my_actions.get(j.url, []) %}
   <a class="{{ 'on' if 'bookmarked' in acts }}" title="Bookmark"
    href="{{ action_url(j.url, 'bookmarked', 'bookmarked' not in acts) }}">{{ '🔖' if 'bookmarked' in acts else '📑' }}</a>
   <a class="{{ 'on' if 'applied' in acts }}" title="Mark applied"
    href="{{ action_url(j.url, 'applied', 'applied' not in acts) }}">{{ '✅' if 'applied' in acts else '⬜' }}</a>
   <a title="{{ 'Unhide' if 'hidden' in acts else 'Hide' }}"
    href="{{ action_url(j.url, 'hidden', 'hidden' not in acts) }}">{{ '👁' if 'hidden' in acts else '✕' }}</a>
  </td></tr>{% endfor %}
 </table>
 {% if pages > 1 %}<div class="pager">
  {% if page > 1 %}<a href="{{ page_url(page-1) }}">← Prev</a>{% endif %}
  <span>Page {{ page }} of {{ pages }}</span>
  {% if page < pages %}<a href="{{ page_url(page+1) }}">Next →</a>{% endif %}
 </div>{% endif %}
{% endif %}
</main>
<script>
 // "Select all" ticks/unticks every portal, and reflects the current state (partial = indeterminate)
 const all = document.getElementById("all-sources"), boxes = [...document.querySelectorAll("input.source")];
 const sync = () => { const n = boxes.filter(b => b.checked).length;
   all.checked = n === boxes.length; all.indeterminate = n > 0 && n < boxes.length; };
 all.addEventListener("change", () => { boxes.forEach(b => b.checked = all.checked); sync(); });
 boxes.forEach(b => b.addEventListener("change", sync)); sync();
</script>
</body></html>"""

SAVED_SEARCHES_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Saved searches — Job Scraper</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1c1e21}
 header{background:#1a73e8;color:#fff;padding:16px 24px}
 header a{color:#e8f0fe;text-decoration:none}
 main{padding:16px 24px;max-width:900px} .card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 18px;margin-bottom:12px}
 .muted{color:#777;font-size:13px} a.btn{display:inline-block;margin-right:10px;padding:6px 14px;border-radius:6px;
   background:#1a73e8;color:#fff;text-decoration:none;font-size:13px}
 a.btn.danger{background:#c5221f}
</style></head><body>
<header><a href="{{ url_for('index') }}">← Job Scraper</a> · <b>Saved searches</b></header>
<main>
{% if not searches %}<p class="muted">No saved searches yet. Run a search and click "🔔 Save &amp; alert me".</p>{% endif %}
{% for s in searches %}<div class="card">
 <b>{{ s.query.get('role', '') }}</b> — {{ s.query.get('location', '') or 'anywhere' }}
 <p class="muted">
   Webhook: {{ s.webhook_url or '(none - won\\'t notify anywhere, just tracks new jobs)' }}<br>
   Last checked: {{ s.last_checked_str }}
 </p>
 <a class="btn" href="{{ url_for('check_saved_search', sid=s.id) }}">Check now</a>
 <a class="btn danger" href="{{ url_for('delete_saved_search', sid=s.id) }}"
   onclick="return confirm('Delete this saved search?')">Delete</a>
</div>{% endfor %}
</main></body></html>"""

STATUS_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Status — Job Scraper</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1c1e21}
 header{background:#1a73e8;color:#fff;padding:16px 24px} header a{color:#e8f0fe;text-decoration:none}
 main{padding:16px 24px;max-width:900px} table{width:100%;border-collapse:collapse;background:#fff;margin-bottom:24px}
 th,td{padding:6px 10px;border-bottom:1px solid #eee;text-align:left;font-size:14px} th{background:#fafafa}
 .ok{color:#137333} .bad{color:#c5221f} h2{font-size:16px}
</style></head><body>
<header><a href="{{ url_for('index') }}">← Job Scraper</a> · <b>Status</b></header>
<main>
<h2>Careers pages</h2>
<table><tr><td>Companies configured</td><td>{{ companies }}</td></tr>
<tr><td>Boards cached</td><td>{{ careers.cached }}</td></tr>
<tr><td>Currently loading</td><td>{{ careers.loading }} ({{ careers.done }}/{{ careers.total }})</td></tr>
<tr><td>Boards failing recently</td><td class="{{ 'bad' if careers.failed_recently else 'ok' }}">{{ careers.failed_recently }}</td></tr></table>
<h2>Daily API quotas</h2>
<table><tr><th>Source</th><th>Used</th><th>Limit</th><th>Remaining</th></tr>
{% for src, u in quota.items() %}<tr><td>{{ src }}</td><td>{{ u.used }}</td><td>{{ u.limit }}</td>
 <td class="{{ 'bad' if u.remaining == 0 else 'ok' }}">{{ u.remaining }}</td></tr>{% endfor %}</table>
<h2>Source health (this run)</h2>
<table><tr><th>Source</th><th>Last count</th><th>Total calls</th><th>Total errors</th><th>Last error</th></tr>
{% for src, s in stats.items()|sort %}<tr><td>{{ src }}</td><td>{{ s.last_count }}</td><td>{{ s.total_calls }}</td>
 <td class="{{ 'bad' if s.total_errors else 'ok' }}">{{ s.total_errors }}</td><td class="muted">{{ s.last_error or '' }}</td></tr>{% endfor %}</table>
</main></body></html>"""


def get_visitor_id() -> str:
    vid = request.cookies.get("visitor_id")
    if not vid:
        vid = store.new_visitor_id()
        request.new_visitor_id = vid  # picked up by after_request to set the cookie
    return vid


@app.after_request
def _set_visitor_cookie(resp):
    vid = getattr(request, "new_visitor_id", None)
    if vid:
        resp.set_cookie("visitor_id", vid, max_age=3650 * 24 * 3600, samesite="Lax")
    return resp


def read_query(args) -> dict:
    return {
        "role": args.get("role", ""),
        "experience": args.get("experience", ""),
        "location": args.get("location", ""),
        "country_code": "",  # no UI control for this; search_jobs auto-detects India and sets gl=in itself
        "date": args.get("date", "") if args.get("date", "") in DATE_WINDOWS else "",
        "sources": args.getlist("sources") or list(SOURCES),
        "strict": bool(args.get("strict")),
        "show_hidden": bool(args.get("show_hidden")),
        "min_salary": args.get("min_salary", "").strip(),
        "max_salary": args.get("max_salary", "").strip(),
        "job_type": [t for t in args.getlist("job_type") if t in JOB_TYPES],
    }


def run_search(q: dict) -> list:
    key = repr(sorted((k, str(v)) for k, v in q.items() if k != "show_hidden"))
    hit = CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    partial = careers_loading(q)
    jobs = search_jobs(q["role"], q["experience"] or None, q["location"], q["sources"],
                       country_code=q["country_code"] or None, date_restrict=q["date"] or None,
                       strict=q["strict"], min_salary=q["min_salary"] or None,
                       max_salary=q["max_salary"] or None, job_types=q["job_type"])
    if not (partial or careers_loading(q)):  # don't keep results missing careers pages that were still loading
        CACHE[key] = (time.time(), jobs)
        while len(CACHE) > CACHE_MAX:  # dicts keep insertion order: drop the oldest searches
            CACHE.pop(next(iter(CACHE)))
    return jobs


def careers_loading(q: dict) -> bool:
    st = careers.status()
    return "careers" in q["sources"] and (st["loading"] or st["cached"] == 0)


@app.route("/")
def index():
    q = read_query(request.args)
    if not request.args:  # first-ever page load (no search submitted yet): show all job types as selected
        q["job_type"] = ["full-time", "internship"]  # not "remote" too - that would narrow, not include-all
    vid = get_visitor_id()
    my_actions = store.get_actions(vid)
    jobs, error, total = None, None, 0
    if q["role"]:
        try:
            jobs = run_search(q)
        except ValueError as e:  # bad experience / salary input
            error = str(e)
    cur = "INR" if is_india(q["location"].split(",")) else "USD"
    sort = request.args.get("sort", "relevance")
    page = max(1, request.args.get("page", 1, type=int))
    pages = 1
    if jobs is not None:
        jobs = sort_jobs(jobs, sort, cur)  # after the cache, so re-sorting never re-scrapes
        if not q["show_hidden"]:
            jobs = [j for j in jobs if "hidden" not in my_actions.get(j.url, ())]
        total = len(jobs)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        jobs = jobs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    def sort_url(value: str) -> str:
        args = request.args.to_dict(flat=False)
        args["sort"] = [value]
        return url_for("index", **args)

    def page_url(value: int) -> str:
        args = request.args.to_dict(flat=False)
        args["page"] = [str(value)]
        return url_for("index", **args)

    def action_url(job_url: str, kind: str, on: bool) -> str:
        return url_for("do_action", job_url=job_url, type=kind, on="1" if on else "0",
                       next=request.full_path)

    careers_note = None
    if jobs is not None and careers_loading(q):
        st = careers.status()
        careers_note = (f"Company careers pages are still loading ({st['done']} of {st['total'] or '…'} companies). "
                        "These results only include the ones loaded so far. Search again in a minute for all of them.")
    return render_template_string(PAGE, q=q, jobs=jobs, total=total, error=error, cur=cur,
                                  sources=list(SOURCES), careers_note=careers_note, my_actions=my_actions,
                                  levels=list(LEVELS), date_windows=DATE_WINDOWS, job_types=JOB_TYPES,
                                  sorts=SORTS, sort=sort, sort_url=sort_url, page=page, pages=pages,
                                  page_url=page_url, action_url=action_url,
                                  fmt_exp=fmt_exp, fmt_salary=fmt_salary, fmt_type=fmt_type,
                                  fmt_posted=fmt_posted)


@app.route("/action")
def do_action():
    vid = get_visitor_id()
    job_url = request.args.get("job_url", "")
    kind = request.args.get("type", "")
    on = request.args.get("on", "1") == "1"
    if job_url and kind in ("bookmarked", "applied", "hidden"):
        store.set_action(vid, job_url, kind, on=on)
    return redirect(request.args.get("next") or url_for("index"))


@app.route("/save-search", methods=["POST"])
def save_search():
    vid = get_visitor_id()
    q = read_query(request.form)
    if q["role"]:
        store.add_saved_search(vid, q, request.form.get("webhook_url", "").strip())
    return redirect(url_for("saved_searches"))


@app.route("/saved-searches")
def saved_searches():
    vid = get_visitor_id()
    searches = store.list_saved_searches(vid)
    for s in searches:
        s["last_checked_str"] = ("never" if not s["last_checked_at"]
                                 else datetime.fromtimestamp(s["last_checked_at"]).strftime("%Y-%m-%d %H:%M"))
    return render_template_string(SAVED_SEARCHES_PAGE, searches=searches)


@app.route("/saved-searches/<sid>/delete")
def delete_saved_search(sid):
    store.remove_saved_search(sid)
    return redirect(url_for("saved_searches"))


@app.route("/saved-searches/<sid>/check")
def check_saved_search(sid):
    matches = [s for s in store.list_saved_searches() if s["id"] == sid]
    if matches:
        alerts._run_one(matches[0])
    return redirect(url_for("saved_searches"))


@app.route("/status")
def status_page():
    return render_template_string(STATUS_PAGE, careers=careers.status(), stats=stats.snapshot(),
                                  quota=quota.usage(), companies=len(careers.load_companies()))


@app.route("/api/search")
def api_search():
    """JSON equivalent of the search page, for programmatic use: /api/search?role=...&location=..."""
    q = read_query(request.args)
    if not q["role"]:
        return jsonify({"error": "role is required"}), 400
    try:
        jobs = run_search(q)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    cur = "INR" if is_india(q["location"].split(",")) else "USD"
    jobs = sort_jobs(jobs, request.args.get("sort"), cur)
    return jsonify({"count": len(jobs), "currency": cur, "jobs": [asdict(j) for j in jobs]})


@app.route("/download.csv")
def download_csv():
    q = read_query(request.args)
    if not q["role"]:
        return "Missing role", 400
    try:
        jobs = run_search(q)
    except ValueError as e:
        return str(e), 400
    cur = "INR" if is_india(q["location"].split(",")) else "USD"
    buf = io.StringIO()
    write_csv(sort_jobs(jobs, request.args.get("sort"), cur), buf)
    name = "jobs-" + "-".join(filter(None, [slugify(q["role"]), slugify(q["location"])])) \
           + datetime.now().strftime("-%Y%m%d") + ".csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",  # BOM so Excel reads UTF-8
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def keep_careers_warm(every: int = 60 * 60) -> None:
    """Company boards take ~30-60 s to download, so load them in the background and keep them fresh;
    searches then read from memory (careers.BOARD_TTL is 75 min)."""
    def loop():
        while True:
            t = time.time()
            for key, (_, jobs) in list(careers._board_cache.items()):  # expire, but keep as fallback
                careers._board_cache[key] = (0, jobs)
            jobs = careers.fetch_all()
            print(f"[careers] loaded {len(jobs)} jobs from {len(careers.load_companies())} companies "
                  f"in {time.time() - t:.0f}s", flush=True)
            time.sleep(every)

    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    # Debug mode runs a second, auto-reloading copy of the app, which roughly doubles memory use.
    # It's off unless asked for:  $env:JOB_SCRAPER_DEBUG = "1"; python app.py
    debug = os.environ.get("JOB_SCRAPER_DEBUG") == "1"
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":  # with debug: only in the worker copy
        keep_careers_warm()
        alerts.start()
    app.run(debug=debug, threaded=True)
