"""
Company careers pages.

Most companies host their careers page on an applicant-tracking system (ATS) that exposes a free public
job-board API. This module reads those boards directly:

    Greenhouse       boards-api.greenhouse.io/v1/boards/<slug>/jobs
    Lever            api.lever.co/v0/postings/<slug>
    Ashby            api.ashbyhq.com/posting-api/job-board/<slug>
    SmartRecruiters  api.smartrecruiters.com/v1/companies/<id>/postings

Which companies to read is kept in companies.json. Manage it from the command line:

    python careers.py list
    python careers.py add "Razorpay" "Postman" "Notion"     # finds which ATS each one uses
    python careers.py remove razorpay
"""

from __future__ import annotations

import html
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import os

import requests

import stats

COMPANIES_FILE = Path(__file__).with_name("companies.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
TIMEOUT = 20
BOARD_TTL = 75 * 60  # boards don't depend on the search, so reuse them across searches for a while
_board_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_failed: dict[tuple[str, str], float] = {}
RETRY_FAILED_AFTER = 5 * 60

# Set LOW_MEMORY=1 on a constrained host (e.g. Streamlit Community Cloud's ~1GB free tier) to cap
# how many jobs are kept per company board. The few dozen large multinationals (Workday especially)
# are what push total memory up; capping just those, rather than dropping companies outright, keeps
# broad coverage while bounding the worst-case size.
LOW_MEMORY = os.getenv("LOW_MEMORY") == "1"
MAX_JOBS_PER_BOARD = {
    "smartrecruiters": 100 if LOW_MEMORY else 300,
    "workday": 40 if LOW_MEMORY else 100,
    "amazon": 100 if LOW_MEMORY else 300,
    "oraclehcm": 60 if LOW_MEMORY else 200,
}


def _text(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ---------------------------------------------------------------------------
# One reader per ATS. Each returns a list of plain dicts (see _job()).
# ---------------------------------------------------------------------------

# Descriptions are only used to find years of experience and pay, so keep just the phrases that mention them
# (~300 chars) instead of the whole text: ~18k boards jobs x 3 KB of description was most of the app's memory.
_FACT_RE = re.compile(  # anchors only; the snippet around each is cut by slicing, which is much faster
    r"\d\s*\+?\s*(?:-|–|to)?\s*\d*\s*\+?\s*(?:years?|yrs?)\b|[$₹€£]\s?\d"
    r"|\b(?:INR|USD|EUR|GBP|LPA|CTC|lakhs?|salary|compensation|intern(?:ship)?)\b", re.IGNORECASE)


def _facts(text: str, limit: int = 320) -> str:
    if not text:
        return ""
    spans, size = [], 0
    for m in _FACT_RE.finditer(text):
        start, end = max(0, m.start() - 50), min(len(text), m.end() + 70)
        if spans and start <= spans[-1][1]:  # overlaps the previous snippet: extend it
            size += end - spans[-1][1]
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
            size += end - start
        if size >= limit:
            break
    return " … ".join(" ".join(text[s:e].split()) for s, e in spans)


_intern = sys.intern  # company / location / type strings repeat thousands of times


def _job(company, title, location, url, posted="", description="", job_type="", remote=False,
         salary=None, department=""):
    return {"company": _intern(company or ""), "title": title, "location": _intern(location or ""), "url": url,
            "posted": str(posted or ""), "description": _facts(description), "job_type": _intern(job_type or ""),
            "remote": bool(remote), "salary": salary, "department": _intern(department or "")}


def greenhouse(slug: str, name: str) -> list[dict]:
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(_job(name, j.get("title", ""), loc, j.get("absolute_url", ""),
                        posted=j.get("first_published") or j.get("updated_at"),
                        remote="remote" in loc.lower()))
    return out


def lever(slug: str, name: str) -> list[dict]:
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"},
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        pay = j.get("salaryRange") or {}
        salary = (pay.get("min"), pay.get("max"), pay.get("currency"), (pay.get("interval") or "").split("-")[-1]) \
            if pay.get("min") or pay.get("max") else None
        out.append(_job(name, j.get("text", ""), ", ".join(filter(None, locs)), j.get("hostedUrl", ""),
                        posted=j.get("createdAt"), description=j.get("descriptionPlain", ""),
                        job_type=cat.get("commitment", ""), remote=j.get("workplaceType") == "remote",
                        salary=salary, department=cat.get("team") or cat.get("department")))
    return out


def ashby(slug: str, name: str) -> list[dict]:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                     params={"includeCompensation": "true"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        out.append(_job(name, j.get("title", ""), ", ".join(filter(None, locs)), j.get("jobUrl", ""),
                        posted=j.get("publishedAt"), description=comp + " " + (j.get("descriptionPlain") or ""),
                        job_type=j.get("employmentType", ""), remote=j.get("isRemote") or
                        (j.get("workplaceType") or "").lower() == "remote", department=j.get("department")))
    return out


def smartrecruiters(slug: str, name: str, max_jobs: int = MAX_JOBS_PER_BOARD["smartrecruiters"]) -> list[dict]:
    out, offset = [], 0
    while offset < max_jobs:
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                         params={"limit": 100, "offset": offset}, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        content = r.json().get("content", [])
        for j in content:
            loc = j.get("location") or {}
            out.append(_job(name, j.get("name", ""),
                            loc.get("fullLocation") or ", ".join(filter(None, [loc.get("city"), loc.get("country")])),
                            f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                            posted=j.get("releasedDate"), job_type=(j.get("typeOfEmployment") or {}).get("label", ""),
                            remote=loc.get("remote"), department=(j.get("department") or {}).get("label")))
        if len(content) < 100:
            break
        offset += 100
    return out


def _find_india_facet(facets: list) -> tuple[str, list[str]] | None:
    """Workday facet (parameter, value ids) that limits a search to India. Facet names differ per company
    (locationCountry, locationHierarchy1, locations, ...), so look for a value named "India", else for
    India location values such as "Bengaluru, India"."""
    exact, partial, counts = None, {}, {}

    def walk(items, param):
        nonlocal exact
        for v in items or []:
            if "values" in v:  # nested group of values under its own parameter
                walk(v["values"], v.get("facetParameter") or param)
                continue
            d = (v.get("descriptor") or "").strip()
            if d.lower() == "india" and exact is None:
                exact = (param, [v["id"]])
            # site-specific labels: "IND-Bangalore ...", "IND19-01-Bengaluru-...", "Bengaluru", "Bangalore, In"
            elif _INDIA_RE.search(d) or _IND_CODE_RE.search(d):
                partial.setdefault(param, []).append(v["id"])
                counts[param] = counts.get(param, 0) + (v.get("count") or 1)

    for f in facets or []:
        walk(f.get("values"), f.get("facetParameter"))
    if exact:
        return exact
    if partial:
        param = max(partial, key=lambda p: counts[p])
        return param, partial[param]
    return None


def workday(slug: str, name: str, max_jobs: int = MAX_JOBS_PER_BOARD["workday"]) -> list[dict]:  # 20/request is Workday's max
    """Workday careers site, India jobs only (these are big global boards). slug = "tenant/wdN/Site",
    from a careers URL like https://tenant.wdN.myworkdayjobs.com/Site."""
    tenant, wd, site = slug.split("/", 2)
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    r = requests.post(api, json=body, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    india = _find_india_facet(r.json().get("facets"))
    if not india:
        return []  # no India openings right now
    body["appliedFacets"] = {india[0]: india[1]}
    out = []
    while body["offset"] < max_jobs:
        r = requests.post(api, json=body, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        postings = r.json().get("jobPostings", [])
        for j in postings:
            loc = j.get("locationsText") or ""
            if "india" not in loc.lower():  # "3 Locations", "Bengaluru" -> we know it's India from the filter
                loc = f"{loc}, India" if loc and "location" not in loc.lower() else "India (multiple locations)"
            out.append(_job(name, j.get("title", ""), loc, base + "/" + site + (j.get("externalPath") or ""),
                            posted=j.get("postedOn", ""), remote="remote" in loc.lower()))
        if len(postings) < 20:
            break
        body["offset"] += 20
    return out


def amazon(slug: str, name: str, max_jobs: int = MAX_JOBS_PER_BOARD["amazon"]) -> list[dict]:
    """amazon.jobs public search, India jobs only (newest first). slug is unused."""
    from datetime import datetime

    out, offset = [], 0
    while offset < max_jobs:
        r = requests.get("https://www.amazon.jobs/en/search.json", headers=HEADERS, timeout=TIMEOUT,
                         params={"country": "IND", "result_limit": 100, "offset": offset, "sort": "recent"})
        r.raise_for_status()
        jobs = r.json().get("jobs", [])
        for j in jobs:
            try:
                posted = datetime.strptime(j.get("posted_date", ""), "%B %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                posted = ""
            loc = (j.get("normalized_location") or j.get("location") or "").replace(", IND", ", India")
            out.append(_job(name, j.get("title", ""), loc, "https://www.amazon.jobs" + (j.get("job_path") or ""),
                            posted=posted, description=(j.get("basic_qualifications") or "").replace("<br/>", ". "),
                            job_type=j.get("job_schedule_type", ""), department=j.get("business_category")))
        if len(jobs) < 100:
            break
        offset += 100
    return out


def oraclehcm(slug: str, name: str, max_jobs: int = MAX_JOBS_PER_BOARD["oraclehcm"]) -> list[dict]:
    """Oracle Recruiting Cloud careers site, India jobs only. slug = "host/SiteNumber", from a careers URL like
    https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001."""
    host, site = slug.split("/", 1)
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    def query(extra: str) -> dict:
        r = requests.get(api, headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT,
                         params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                                 "finder": f"findReqs;siteNumber={site},facetsList=LOCATIONS,{extra}"})
        r.raise_for_status()
        return (r.json().get("items") or [{}])[0]

    first = query("limit=1")
    india = next((l["Id"] for l in first.get("locationsFacet") or [] if (l.get("Name") or "").strip() == "India"), None)
    # the facet only lists the biggest locations; otherwise search by location name and keep Indian postings
    india_filter = f"selectedLocationsFacet={india}" if india else "location=India"
    out, offset = [], 0
    while offset < max_jobs:
        page = query(f"limit=25,offset={offset},{india_filter},sortBy=POSTING_DATES_DESC")
        reqs = page.get("requisitionList") or []
        for j in reqs:
            if not india and j.get("PrimaryLocationCountry") != "IN":
                continue
            out.append(_job(name, j.get("Title", ""), j.get("PrimaryLocation", ""),
                            f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id')}",
                            posted=j.get("PostedDate", ""),
                            description=f"{j.get('ShortDescriptionStr') or ''} {j.get('ExternalQualificationsStr') or ''}",
                            job_type=j.get("JobSchedule") or j.get("WorkerType") or "",
                            remote="remote" in (j.get("WorkplaceType") or "").lower(), department=j.get("JobFamily")))
        if len(reqs) < 25:
            break
        offset += 25
    return out


ATS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
       "workday": workday, "amazon": amazon, "oraclehcm": oraclehcm}

# For companies marked "india_only" in companies.json (US companies with India offices), keep only their
# India jobs, so their big global boards don't fill memory. Workday boards are always India-only.
_INDIA_RE = re.compile(r"\b(india|bengaluru|bangalore|mumbai|delhi|new delhi|gurgaon|gurugram|noida|hyderabad|pune|"
                       r"chennai|kolkata|ahmedabad|jaipur|kochi|indore|chandigarh|coimbatore|thiruvananthapuram|"
                       r"trivandrum|mysore|mysuru|vadodara|nagpur|bhubaneswar|visakhapatnam)\b", re.IGNORECASE)


# Workday location codes: "IND-Bangalore", "IND19-01-Bengaluru", "IN-KA-Bangalore" (not ", IN": that is Indiana)
_IND_CODE_RE = re.compile(r"^IND\d*\s*[-_ ]\s*\w|^IN\s*[-_ ]\s*(?:KA|MH|TN|TS|TG|AP|DL|HR|UP|GJ|WB|KL|RJ|OR|OD|PB|CH|MP)\b",
                          re.IGNORECASE)


def _india_only(jobs: list[dict]) -> list[dict]:
    return [j for j in jobs if _INDIA_RE.search(j["location"])]


# ---------------------------------------------------------------------------
# Company list
# ---------------------------------------------------------------------------

def load_companies() -> list[dict]:
    try:
        return json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def save_companies(companies: list[dict]) -> None:
    companies = sorted({c["slug"].lower() + c["ats"]: c for c in companies}.values(), key=lambda c: c["name"].lower())
    COMPANIES_FILE.write_text(json.dumps(companies, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch_board(company: dict) -> list[dict]:
    key = (company["ats"], company["slug"])
    hit = _board_cache.get(key)
    if hit and time.time() - hit[0] < BOARD_TTL:
        return hit[1]
    if not hit and time.time() - _failed.get(key, 0) < RETRY_FAILED_AFTER:
        return []  # failed recently: don't make every search wait for its timeout again
    for attempt in range(2):  # one retry: connection resets are common when many boards load at once
        try:
            jobs = ATS[company["ats"]](company["slug"], company["name"])
            if company.get("india_only"):
                jobs = _india_only(jobs)
            break
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 0:
                time.sleep(2)
                continue
            _failed[key] = time.time()
            if hit:  # temporary failure: keep serving the last good copy
                return hit[1]
            raise
        except Exception:
            _failed[key] = time.time()
            if hit:
                return hit[1]
            raise
    _board_cache[key] = (time.time(), jobs)
    return jobs


# Only one full download at a time. A second caller (e.g. a search while the web app is still warming up)
# would double the connections and make boards fail, so it waits for or reuses the running download.
_fetch_lock = threading.Lock()
_progress = {"loading": False, "done": 0, "total": 0}


def status() -> dict:
    """{'loading': bool, 'done': boards finished, 'total': boards, 'cached': boards with data,
    'failed_recently': boards that errored within the last RETRY_FAILED_AFTER window}"""
    now = time.time()
    failed_recently = sum(1 for t in _failed.values() if now - t < RETRY_FAILED_AFTER)
    return {**_progress, "cached": len(_board_cache), "failed_recently": failed_recently}


def cached_jobs() -> list[dict]:
    """Whatever is loaded right now, without downloading anything."""
    return [j for _, jobs in list(_board_cache.values()) for j in jobs]


def fetch_all(companies: list[dict] | None = None, workers: int = 6) -> list[dict]:
    """All jobs from every company board, a few at a time. Broken boards are skipped."""
    companies = load_companies() if companies is None else companies
    # Workday needs several requests per company, so load the quick single-request boards first
    companies = sorted(companies, key=lambda c: c["ats"] == "workday")

    def one(c):
        try:
            return fetch_board(c)
        except Exception as e:
            print(f"[careers] {c['name']} ({c['ats']}) failed: {e!s:.120}", file=sys.stderr)
            return []
        finally:
            _progress["done"] += 1

    with _fetch_lock:
        _progress.update(loading=True, done=0, total=len(companies))
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                return [j for batch in pool.map(one, companies) for j in batch]
        finally:
            _progress["loading"] = False


def discover(name: str) -> dict | None:
    """Find which ATS a company uses by trying likely slugs on each. Picks the board with the most jobs."""
    base = re.sub(r"[^a-z0-9]", "", name.lower())
    dashed = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slugs = list(dict.fromkeys([base, dashed, base + "hq", base + "inc", base + "careers"]))
    best = None
    for ats, fn in ATS.items():
        variants = slugs + ([name.replace(" ", ""), base.capitalize()] if ats == "smartrecruiters" else [])
        for slug in dict.fromkeys(variants):
            try:
                n = len(fn(slug, name))
            except Exception:
                continue
            if n and (best is None or n > best["jobs"]):
                best = {"name": name, "ats": ats, "slug": slug, "jobs": n}
    return best


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "list"), sys.argv[2:]
    companies = load_companies()
    if cmd == "list":
        for c in companies:
            print(f"{c['name']:32} {c['ats']:16} {c['slug']}")
        print(f"\n{len(companies)} companies")
    elif cmd == "add":
        for name in args:
            found = discover(name)
            if found:
                print(f"+ {name}: {found['ats']}/{found['slug']} ({found['jobs']} open jobs)")
                companies.append({k: found[k] for k in ("name", "ats", "slug")})
            else:
                print(f"- {name}: no public Greenhouse/Lever/Ashby/SmartRecruiters board found")
        save_companies(companies)
    elif cmd == "remove":
        drop = {a.lower() for a in args}
        save_companies([c for c in companies if c["slug"].lower() not in drop and c["name"].lower() not in drop])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
