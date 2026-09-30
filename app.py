"""Web UI for the job scraper.  Run:  python app.py  ->  http://127.0.0.1:5000"""

import io
import os
import threading
import time
from datetime import datetime

from flask import Flask, Response, render_template_string, request, url_for

import careers
from job_scraper import (DATE_WINDOWS, JOB_TYPES, LEVELS, SORTS, SOURCES, fmt_exp, fmt_posted, fmt_salary,
                         fmt_type, is_india, search_jobs, slugify, sort_jobs, write_csv)

app = Flask(__name__)

# Last results per query, so "Download CSV" doesn't re-scrape every source.
CACHE: dict[str, tuple[float, list]] = {}
CACHE_TTL = 15 * 60
CACHE_MAX = 20  # searches kept for re-sorting / CSV download

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Scraper</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1c1e21}
 header{background:#1a73e8;color:#fff;padding:16px 24px}
 form{display:flex;flex-wrap:wrap;gap:12px;padding:16px 24px;background:#fff;border-bottom:1px solid #ddd}
 label{display:flex;flex-direction:column;font-size:12px;gap:4px;color:#555}
 input,select{padding:8px;border:1px solid #ccc;border-radius:6px;font-size:14px;min-width:160px}
 button{align-self:end;padding:9px 20px;background:#1a73e8;color:#fff;border:0;border-radius:6px;cursor:pointer}
 .src{display:flex;gap:10px;align-items:center;font-size:13px}
 main{padding:16px 24px} table{width:100%;border-collapse:collapse;background:#fff}
 th,td{padding:8px 10px;border-bottom:1px solid #eee;text-align:left;font-size:14px;vertical-align:top}
 th{background:#fafafa}
 .bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
 .btn{padding:8px 16px;background:#188038;color:#fff;border-radius:6px;font-size:14px}
 .btn:hover{background:#137333}
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
 .actions{display:flex;gap:10px;align-items:center} .actions select{min-width:0}
 th a{color:inherit} th a:hover{color:#1a73e8}
</style></head><body>
<header><b>Job Scraper</b> — Google Custom Search + free job APIs</header>
<form method="get" id="search">
 <label>Role<input name="role" value="{{ q.role }}" placeholder="python developer" required></label>
 <label>Experience
  <select name="experience">
   <option value="">Any</option>
   {% for e in ["0-1","0-2","2-4","3-5","5+","8+"] + levels %}
   <option {{ 'selected' if q.experience==e }}>{{ e }}</option>{% endfor %}
  </select></label>
 <label>Location(s)<input name="location" value="{{ q.location }}" placeholder="india, berlin, remote"></label>
 <label>Google country (gl)<input name="country_code" value="{{ q.country_code }}" placeholder="in / us / de" style="min-width:80px"></label>
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
   {{ 'checked' if q.strict }}>strict experience</label></div>
 <button>Search</button>
</form>
<main>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if careers_note %}<p class="note">{{ careers_note }}</p>{% endif %}
{% if jobs is not none %}
 <div class="bar"><p>{{ jobs|length }} jobs found</p>
  {% if jobs %}<div class="actions">
   <label style="flex-direction:row;align-items:center;gap:6px">Sort
    <select name="sort" form="search" onchange="this.form.submit()">{% for v, t in sorts.items() %}
     <option value="{{ v }}" {{ 'selected' if sort==v }}>{{ t }}</option>{% endfor %}</select></label>
   <a class="btn" href="/download.csv?{{ request.query_string.decode() }}">⬇ Download CSV</a>
  </div>{% endif %}</div>
 <table><tr><th>Title</th><th>Company</th><th>Location</th><th>Experience</th><th><a href="{{ sort_url('salary_asc' if sort=='salary_desc' else 'salary_desc') }}"
   title="Sort by salary">Salary / yr {{ '▼' if sort=='salary_desc' else '▲' if sort=='salary_asc' else '↕' }}</a></th><th>Type</th><th>Posted</th><th>Source</th></tr>
 {% for j in jobs %}<tr>
  <td><a href="{{ j.url }}" target="_blank" rel="noopener">{{ j.title }}</a></td>
  <td>{{ j.company }}</td><td>{{ j.location }}</td><td>{{ fmt_exp(j) }}</td>
  <td class="salary">{{ fmt_salary(j, cur) }}</td>
  <td class="type">{% for t in fmt_type(j).split(" · ") if t != "-" %}<span class="tag {{ t|lower }}">{{ t }}</span> {% endfor %}</td>
  <td class="muted" title="{{ j.posted }}">{{ fmt_posted(j) }}</td><td class="muted">{{ j.source }}</td></tr>{% endfor %}
 </table>
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


def read_query(args) -> dict:
    return {
        "role": args.get("role", ""),
        "experience": args.get("experience", ""),
        "location": args.get("location", ""),
        "country_code": args.get("country_code", ""),
        "date": args.get("date", "") if args.get("date", "") in DATE_WINDOWS else "",
        "sources": args.getlist("sources") or list(SOURCES),
        "strict": bool(args.get("strict")),
        "min_salary": args.get("min_salary", "").strip(),
        "max_salary": args.get("max_salary", "").strip(),
        "job_type": [t for t in args.getlist("job_type") if t in JOB_TYPES],
    }


def run_search(q: dict) -> list:
    key = repr(sorted((k, str(v)) for k, v in q.items()))
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
    jobs, error = None, None
    if q["role"]:
        try:
            jobs = run_search(q)
        except ValueError as e:  # bad experience / salary input
            error = str(e)
    cur = "INR" if is_india(q["location"].split(",")) else "USD"
    sort = request.args.get("sort", "relevance")
    if jobs:
        jobs = sort_jobs(jobs, sort, cur)  # after the cache, so re-sorting never re-scrapes

    def sort_url(value: str) -> str:
        args = request.args.to_dict(flat=False)
        args["sort"] = [value]
        return url_for("index", **args)

    careers_note = None
    if jobs is not None and careers_loading(q):
        st = careers.status()
        careers_note = (f"Company careers pages are still loading ({st['done']} of {st['total'] or '…'} companies). "
                        "These results only include the ones loaded so far. Search again in a minute for all of them.")
    return render_template_string(PAGE, q=q, jobs=jobs, error=error, cur=cur, sources=list(SOURCES),
                                  careers_note=careers_note,
                                  levels=list(LEVELS), date_windows=DATE_WINDOWS, job_types=JOB_TYPES,
                                  sorts=SORTS, sort=sort, sort_url=sort_url,
                                  fmt_exp=fmt_exp, fmt_salary=fmt_salary, fmt_type=fmt_type,
                                  fmt_posted=fmt_posted)


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
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",  # BOM so Excel reads UTF-8
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
    app.run(debug=debug, threaded=True)
