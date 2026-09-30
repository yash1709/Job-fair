"""
Worldwide job scraper.

Primary source: Google Programmable Search (Custom Search JSON API, free tier =
100 queries/day). It searches the public job pages of the big applicant-tracking
systems (Greenhouse, Lever, Workday, Ashby, LinkedIn, ...) worldwide.

Extra keyless sources (optional): Remotive, Arbeitnow, RemoteOK, Jobicy.

Filters: role, experience (years range or level), location.

Usage:
    python job_scraper.py --role "data engineer" --experience 3-5 --location "germany"
    python job_scraper.py --role "react developer" --experience senior --location remote --out jobs.csv
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from datetime import datetime, timedelta, timezone

import requests

import quota
import stats

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

HEADERS = {"User-Agent": "Mozilla/5.0 (job-scraper; +https://github.com/)"}
TIMEOUT = 20

GOOGLE_JOB_SITES = [
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "myworkdayjobs.com",
    "jobs.smartrecruiters.com",
    "apply.workable.com",
    "linkedin.com/jobs/view",
    "wellfound.com/jobs",
]

# Used instead of the global list when the location filter is in India
# (Google caps queries at ~32 terms, so the two lists can't be combined).
GOOGLE_INDIA_JOB_SITES = [
    "naukri.com/job-listings",
    "foundit.in/job",
    "instahyre.com/job",
    "cutshort.io/job",
    "iimjobs.com/j",
    "hirist.tech/j",
    "internshala.com/job/detail",
    "shine.com/jobs",
    "in.indeed.com/viewjob",
    "linkedin.com/jobs/view",
]

INDIA_CITIES = [
    "india", "bangalore", "bengaluru", "mumbai", "navi mumbai", "delhi", "new delhi", "ncr", "gurgaon",
    "gurugram", "noida", "greater noida", "hyderabad", "secunderabad", "chennai", "pune", "kolkata",
    "ahmedabad", "gandhinagar", "jaipur", "chandigarh", "mohali", "kochi", "cochin", "thiruvananthapuram",
    "trivandrum", "coimbatore", "indore", "bhopal", "lucknow", "nagpur", "surat", "vadodara", "visakhapatnam",
    "vizag", "bhubaneswar", "mysore", "mysuru", "mangalore", "thane", "nashik", "patna", "dehradun",
]

# Location aliases so "bangalore" also matches "Bengaluru", "india" matches Indian cities, etc.
LOCATION_ALIASES = {
    "india": INDIA_CITIES,
    "bangalore": ["bangalore", "bengaluru"],
    "bengaluru": ["bangalore", "bengaluru"],
    "gurgaon": ["gurgaon", "gurugram"],
    "gurugram": ["gurgaon", "gurugram"],
    "delhi": ["delhi", "ncr", "noida", "gurgaon", "gurugram"],
    "ncr": ["delhi", "ncr", "noida", "gurgaon", "gurugram"],
    "mumbai": ["mumbai", "navi mumbai", "thane"],
    "kochi": ["kochi", "cochin"],
    "usa": ["usa", "united states", "u.s.", "us-"],
    "uk": ["uk", "united kingdom", "england", "london"],
}


def is_india(locations: list[str]) -> bool:
    return any(l.lower().strip() in INDIA_CITIES for l in locations)

# Seniority level -> (min years, max years)
LEVELS = {
    "intern": (0, 0),
    "entry": (0, 2),
    "junior": (0, 2),
    "mid": (2, 5),
    "senior": (5, 10),
    "lead": (7, 15),
    "staff": (8, 20),
    "principal": (10, 25),
}

TITLE_LEVEL_WORDS = [
    (r"\b(intern|internship|trainee)\b", "intern"),
    (r"\b(junior|jr\.?|entry[- ]level|graduate|associate)\b", "junior"),
    (r"\b(senior|sr\.?)\b", "senior"),
    (r"\b(lead|head of|manager)\b", "lead"),
    (r"\bstaff\b", "staff"),
    (r"\b(principal|distinguished|director)\b", "principal"),
]

YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*\+?)?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)


@dataclass(slots=True)  # slots: far smaller per job object
class Job:
    title: str
    company: str = ""
    location: str = ""
    url: str = ""
    source: str = ""
    description: str = ""
    posted: str = ""
    exp_min: int | None = None
    exp_max: int | None = None
    level: str = ""
    tags: list[str] = field(default_factory=list)
    salary_min: float | None = None  # annual, in salary_currency
    salary_max: float | None = None
    salary_currency: str = ""
    job_type: str = ""  # "full-time", "part-time", "contract", "internship" (comma-joined if several)
    remote: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_experience_arg(value: str | None) -> tuple[int, int] | None:
    """'3-5' -> (3,5), '5+' -> (5,99), '2' -> (2,2), 'senior' -> LEVELS['senior']."""
    if not value:
        return None
    v = value.strip().lower()
    if v in LEVELS:
        return LEVELS[v]
    m = re.fullmatch(r"(\d+)\s*\+", v)
    if m:
        return int(m.group(1)), 99
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", v)
    if m:
        return int(m.group(1)), int(m.group(2))
    if v.isdigit():
        return int(v), int(v)
    raise ValueError(f"Bad experience value '{value}'. Use e.g. 3-5, 5+, 2, or one of {list(LEVELS)}")


def enrich_experience(job: Job) -> None:
    """Detect years of experience and seniority level from title + description."""
    text = f"{job.title} {job.description}"
    for m in ([] if job.exp_min is not None else YEARS_RE.finditer(text)):  # keep source-provided values
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        if lo > 30:
            continue
        job.exp_min = lo
        job.exp_max = hi if hi is not None else 99  # "3+ years" / "3 years" = at least 3
        break
    for pattern, level in ([] if job.level else TITLE_LEVEL_WORDS):
        if re.search(pattern, job.title, re.IGNORECASE):
            job.level = level
            break


ROLE_SYNONYMS = [
    {"developer", "engineer", "programmer", "dev", "swe", "coder"},
    {"frontend", "front-end", "front", "ui"},
    {"backend", "back-end", "back"},
    {"fullstack", "full-stack", "full"},
    {"manager", "lead", "head"},
    {"ml", "machine"},
]


def matches_role(job: Job, role: str) -> bool:
    """Every role word (or a synonym) must appear in the title; skill words may also match tags."""
    if not role:
        return True
    split = lambda s: set(re.split(r"[^\w+#.-]+", s.lower()))
    title = split(job.title)
    title_and_tags = title | split(" ".join(job.tags))
    for word in split(role) - {""}:
        group = next((g for g in ROLE_SYNONYMS if word in g), None)
        if group:  # job-type word like "developer" -> must be in the title
            if not group & title:
                return False
        elif word not in title_and_tags:
            return False
    return True


def matches_experience(job: Job, exp: tuple[int, int] | None, strict: bool) -> bool:
    if not exp:
        return True
    lo, hi = exp
    if job.exp_min is not None:
        return job.exp_min <= hi and (job.exp_max or 99) >= lo
    if job.level:
        llo, lhi = LEVELS[job.level]
        return llo <= hi and lhi >= lo
    # No signal in the posting: keep it unless strict mode.
    return not strict


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------

CURRENCY_SYMBOLS = {"₹": "INR", "rs": "INR", "inr": "INR", "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR",
                    "£": "GBP", "gbp": "GBP", "cad": "CAD", "aud": "AUD", "sgd": "SGD", "aed": "AED"}
PERIOD_MULTIPLIER = {"hour": 2080, "hr": 2080, "day": 260, "week": 52, "month": 12, "mo": 12,
                     "year": 1, "yr": 1, "annum": 1, "annual": 1, "yearly": 1, "monthly": 12, "hourly": 2080}
UNIT = {"k": 1e3, "l": 1e5, "lac": 1e5, "lakh": 1e5, "lakhs": 1e5, "lpa": 1e5, "cr": 1e7, "crore": 1e7, "m": 1e6}
FALLBACK_RATES_PER_USD = {"USD": 1, "INR": 88, "EUR": 0.86, "GBP": 0.75, "CAD": 1.38, "AUD": 1.52,
                          "SGD": 1.29, "AED": 3.67}
_rates: dict[str, float] | None = None

SALARY_RE = re.compile(
    r"(?P<cur>₹|\$|€|£|\b(?:rs\.?|inr|usd|eur|gbp|cad|aud|sgd|aed)\b)?\s*"
    r"(?P<a>\d[\d,]*(?:\.\d+)?)\s*(?P<ua>k|l|lpa|lakhs?|lac|cr|crore|m)?\b\s*"
    r"(?:-|–|to)\s*(?:₹|\$|€|£|rs\.?|inr|usd|eur|gbp)?\s*"
    r"(?P<b>\d[\d,]*(?:\.\d+)?)\s*(?P<ub>k|l|lpa|lakhs?|lac|cr|crore|m)?\b"
    r"(?:\s*(?:/|per|a|an)\s*(?P<per>hour|hr|day|week|month|mo|year|yr|annum))?",
    re.IGNORECASE,
)


def fx_rates() -> dict[str, float]:
    """Currency -> units per USD. Live from open.er-api.com (free, no key), static fallback."""
    global _rates
    if _rates is None:
        try:
            r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
            _rates = {**FALLBACK_RATES_PER_USD, **r.json()["rates"]}
        except Exception:
            _rates = dict(FALLBACK_RATES_PER_USD)
    return _rates


def convert(amount: float | None, src: str, dst: str) -> float | None:
    if amount is None or not src or src == dst:
        return amount
    rates = fx_rates()
    if src not in rates or dst not in rates:
        return None
    return amount / rates[src] * rates[dst]


def parse_amount(value: str | None) -> float | None:
    """'12 LPA' -> 1_200_000, '80k' -> 80_000, '1.5 cr' -> 15_000_000, '100000' -> 100_000."""
    if not value or not str(value).strip():
        return None
    m = re.fullmatch(r"\s*(\d[\d,]*(?:\.\d+)?)\s*([a-z]*)\s*", str(value).lower())
    if not m:
        raise ValueError(f"Bad salary '{value}'. Use e.g. 1200000, 12 LPA, 12L, 80k")
    return float(m.group(1).replace(",", "")) * UNIT.get(m.group(2), 1)


def parse_salary_text(text: str, default_currency: str = "") -> tuple[float, float, str] | None:
    """Find a salary range like '$90k - $105k', '₹ 6,00,000 - 9,50,000 /year', '12-18 LPA', '$50-75/hour'."""
    for m in SALARY_RE.finditer(text or ""):
        cur_raw = (m.group("cur") or "").lower().rstrip(".")
        ua, ub = (m.group("ua") or "").lower(), (m.group("ub") or "").lower()
        if not cur_raw and not (ua or ub) and not re.search(r"salary|ctc|pay|compensation|stipend",
                                                            text[max(0, m.start() - 40):m.start()], re.I):
            continue  # plain "3-5" is probably years, not money
        a = float(m.group("a").replace(",", "")) * UNIT.get(ua or ub, 1)
        b = float(m.group("b").replace(",", "")) * UNIT.get(ub or ua, 1)
        per = (m.group("per") or "").lower()
        mult = PERIOD_MULTIPLIER.get(per, 1)
        if not per and b < 1000:  # "$50-75" without unit is hourly
            mult = 2080
        cur = CURRENCY_SYMBOLS.get(cur_raw) or ("INR" if (ua or ub) in ("l", "lpa", "lakh", "lakhs", "lac", "cr", "crore")
                                                 else default_currency)
        if not cur or b < a or b * mult < 1000:
            continue
        return a * mult, b * mult, cur
    return None


def sal(lo, hi, currency: str | None, period: str | None = "year") -> dict:
    """Job(...) kwargs for a structured salary; empty if missing."""
    mult = PERIOD_MULTIPLIER.get((period or "year").lower(), 1)
    lo = float(lo) * mult if lo not in (None, "", 0, "None") else None
    hi = float(hi) * mult if hi not in (None, "", 0, "None") else None
    if (lo or hi) and currency and currency != "None":
        return {"salary_min": lo, "salary_max": hi, "salary_currency": currency.upper()}
    return {}


def sal_text(text: str | None, default_currency: str = "") -> dict:
    """Job(...) kwargs parsed from a salary string like '$90k - $105k'."""
    found = parse_salary_text(text or "", default_currency)
    return dict(zip(("salary_min", "salary_max", "salary_currency"), found)) if found else {}


def enrich_salary(job: Job, default_currency: str = "") -> None:
    if job.salary_min is None and job.salary_max is None:
        found = parse_salary_text(f"{job.title} {job.description}", default_currency)
        if found:
            job.salary_min, job.salary_max, job.salary_currency = found


def matches_salary(job: Job, min_sal: float | None, max_sal: float | None, currency: str,
                   salary_only: bool) -> bool:
    if job.salary_min is None and job.salary_max is None:
        return not salary_only
    lo = convert(job.salary_min if job.salary_min is not None else job.salary_max, job.salary_currency, currency)
    hi = convert(job.salary_max if job.salary_max is not None else job.salary_min, job.salary_currency, currency)
    if lo is None or hi is None:  # unknown currency
        return not salary_only
    return (min_sal is None or hi >= min_sal) and (max_sal is None or lo <= max_sal)


def fmt_money(amount: float | None, currency: str) -> str:
    if amount is None:
        return "?"
    if currency == "INR":
        return f"{amount / 1e7:.2g}Cr" if amount >= 1e7 else f"{amount / 1e5:.3g}L"
    return f"{amount / 1e3:.0f}k" if amount >= 1000 else f"{amount:.0f}"


def fmt_salary(j: Job, currency: str | None = None) -> str:
    """'₹6L-9.5L', '$90k-105k'. With `currency`, also shows the converted value."""
    if j.salary_min is None and j.salary_max is None:
        return "-"
    sym = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}.get(j.salary_currency, j.salary_currency + " ")
    lo, hi = j.salary_min, j.salary_max
    text = sym + (fmt_money(lo if lo is not None else hi, j.salary_currency) if lo == hi or hi is None or lo is None
                  else f"{fmt_money(lo, j.salary_currency)}-{fmt_money(hi, j.salary_currency)}")
    if currency and currency != j.salary_currency:
        clo = convert(lo if lo is not None else hi, j.salary_currency, currency)
        chi = convert(hi if hi is not None else lo, j.salary_currency, currency)
        if clo is not None:
            csym = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}.get(currency, currency + " ")
            text += f" (≈{csym}{fmt_money(clo, currency)}-{fmt_money(chi, currency)})"
    return text


# ---------------------------------------------------------------------------
# Posted date
# ---------------------------------------------------------------------------

# Same codes as Google's dateRestrict, so one setting drives both.
DATE_WINDOWS = {"d1": "Last 24 hours", "d7": "Last 7 days", "w2": "Last 2 weeks", "m1": "Last month",
                "m3": "Last 3 months"}
_WINDOW_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def window_days(code: str | None) -> float | None:
    m = re.fullmatch(r"([dwmy])(\d+)", (code or "").strip().lower())
    return int(m.group(2)) * _WINDOW_UNIT_DAYS[m.group(1)] if m else None


def iso_from_epoch(value) -> str:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts > 1e12:  # milliseconds
        ts /= 1000
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts > 0 else ""


def iso_from_relative(text: str) -> str:
    """'Just now' / 'Few hours ago' / '2 days ago' / '1 week ago' / '3 months ago' -> ISO timestamp."""
    t = (text or "").lower()
    now = datetime.now(timezone.utc)
    if re.search(r"just now|today|few (minutes|hours)|hour|minute", t):
        delta = timedelta(hours=1)
    elif m := re.search(r"(\d+)\+?\s*(day|week|month)", t):
        delta = timedelta(days=int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)])
    elif "yesterday" in t:
        delta = timedelta(days=1)
    else:
        return ""
    return (now - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def posted_datetime(job: Job) -> datetime | None:
    raw = (job.posted or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{10,13}", raw):
        raw = iso_from_epoch(raw)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def matches_posted(job: Job, days: float | None) -> bool:
    """Undated jobs are dropped once a window is chosen (Google results were already date-limited by Google).
    Date-only values count as the end of that day, so '24 hours' doesn't drop yesterday's postings."""
    if not days or job.source in ("google", "jobvetta"):  # these apply the date window themselves
        return True
    dt = posted_datetime(job)
    if dt is None:
        return False
    if len(job.posted.strip()) == 10:
        dt += timedelta(days=1)
    return dt >= datetime.now(timezone.utc) - timedelta(days=days)


def fmt_posted(job: Job) -> str:
    dt = posted_datetime(job)
    if dt is None:
        return job.posted[:10] or "-"
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if len(job.posted.strip()) > 10 and hours < 24:
        return "today" if hours < 1 else f"{int(hours)}h ago"
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Job type
# ---------------------------------------------------------------------------

JOB_TYPES = ["full-time", "internship", "remote"]
REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|anywhere|worldwide)\b", re.IGNORECASE)
JOB_TYPE_PATTERNS = [  # checked against a source's raw type field, or the title as a fallback
    (r"intern|trainee|apprentice|working student|student", "internship"),
    (r"part[\s_-]?time", "part-time"),
    (r"contract|freelance|temporary|temp\b", "contract"),
    (r"full[\s_-]?time|permanent", "full-time"),
]


def normalize_job_type(*raw) -> str:
    """'Full Time' / 'full_time' / ['Experienced', 'Permanent'] / 'Intern' -> 'full-time' / 'internship' ..."""
    text = " ".join(" ".join(r) if isinstance(r, (list, tuple)) else str(r or "") for r in raw)
    found = [t for pattern, t in JOB_TYPE_PATTERNS if re.search(pattern, text, re.IGNORECASE)]
    return ", ".join(dict.fromkeys(found))


def enrich_job_type(job: Job) -> None:
    if not job.job_type:
        job.job_type = normalize_job_type(re.sub(r"\bintern(al|ational)\w*", "", job.title))
    if not job.remote:
        job.remote = bool(REMOTE_RE.search(f"{job.location} {job.title}"))


def matches_job_type(job: Job, wanted: list[str]) -> bool:
    """full-time / internship are alternatives (OR); remote must also hold (AND).
    Jobs with no stated type count as full-time, since that's what almost all of them are."""
    if not wanted:
        return True
    if "remote" in wanted and not job.remote:
        return False
    kinds = [w for w in wanted if w != "remote"]
    if not kinds:
        return True
    job_kinds = [k.strip() for k in job.job_type.split(",") if k.strip()] or ["full-time"]
    return any(k in job_kinds for k in kinds)


@lru_cache(maxsize=64)
def _location_regex(locations: tuple[str, ...]) -> re.Pattern:
    """One combined pattern for all requested locations and their aliases."""
    terms = []
    for loc in locations:
        loc = loc.lower().strip()
        if loc in ("remote", "anywhere", "worldwide", "wfh"):
            terms += ["remote", "anywhere", "worldwide", "work from home", "wfh"]
        terms += LOCATION_ALIASES.get(loc, [loc])
    terms = sorted({t for t in terms if t}, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(map(re.escape, terms)) + r")\b", re.IGNORECASE)


def matches_location(job: Job, locations: list[str]) -> bool:
    if not locations:
        return True
    pattern = _location_regex(tuple(locations))
    # the location field decides when it's filled in; the title/description only as a fallback
    return bool(pattern.search(job.location) or pattern.search(job.title)
                or (not job.location.strip() and pattern.search(job.description)))


_COMPANY_SUFFIX_RE = re.compile(
    r"\b(pvt\.?|private|ltd\.?|limited|llc|inc\.?|incorporated|corp\.?|corporation|technologies|"
    r"technology|solutions|systems|group)\b", re.IGNORECASE)
_TITLE_NOISE_RE = re.compile(r"\(.*?\)|\[.*?\]|[^\w\s]")


def _fuzzy_key(job: Job) -> tuple[str, str, str] | None:
    """A loose (title, company, city) key for catching the same job posted through more than one
    source - e.g. a company's own careers page and a job board that also indexed it, where the exact
    title/company/location strings differ slightly ("Pvt Ltd" vs not, "Bangalore" vs "Bengaluru, KA").
    None if there isn't enough to key on safely (empty title or company)."""
    title = re.sub(r"\s+", " ", _TITLE_NOISE_RE.sub(" ", job.title.lower())).strip()
    company = re.sub(r"[^\w\s]", "", _COMPANY_SUFFIX_RE.sub("", job.company.lower()))
    company = re.sub(r"\s+", " ", company).strip()
    if not title or not company:
        return None
    city = re.split(r"[,/(]", job.location.lower())[0].strip()
    if city != "india":  # LOCATION_ALIASES["india"] expands to every Indian city (for matching, not
        city = LOCATION_ALIASES.get(city, [city])[0]  # canonicalizing) - leave a bare "india" as-is
    return title, company, city


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def google_search(role: str, experience: str | None, locations: list[str],
                  max_results: int = 50, country_code: str | None = None,
                  date_restrict: str | None = "m1") -> list[Job]:
    """Google Custom Search JSON API. Needs GOOGLE_API_KEY and GOOGLE_CSE_ID."""
    key, cx = os.getenv("GOOGLE_API_KEY"), os.getenv("GOOGLE_CSE_ID")
    if not key or not cx:
        print("[google] GOOGLE_API_KEY / GOOGLE_CSE_ID not set - skipping Google.", file=sys.stderr)
        return []
    if not quota.allow("google"):
        print("[google] daily quota used up - skipping.", file=sys.stderr)
        return []

    site_list = GOOGLE_INDIA_JOB_SITES if is_india(locations) else GOOGLE_JOB_SITES
    if is_india(locations) and not country_code:
        country_code = "in"
    sites = " OR ".join(f"site:{s}" for s in site_list)
    parts = [f'"{role}"'] if role else []
    if locations:
        parts.append("(" + " OR ".join(f'"{l}"' for l in locations) + ")")
    if experience and experience.lower() in LEVELS and experience.lower() not in ("mid",):
        parts.append(experience)
    query = " ".join(parts) + f" ({sites})"

    jobs: list[Job] = []
    start = 1
    while len(jobs) < max_results and start <= 91:  # API caps at 100 results/query
        params = {"key": key, "cx": cx, "q": query, "num": 10, "start": start}
        if country_code:
            params["gl"] = country_code
        if date_restrict:
            params["dateRestrict"] = date_restrict
        r = requests.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"[google] HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
            break
        data = r.json()
        items = data.get("items", [])
        if not items:
            break
        for it in items:
            jobs.append(_google_item_to_job(it))
        start += 10
        time.sleep(0.2)
    return jobs[:max_results]


def _google_item_to_job(it: dict) -> Job:
    link = it.get("link", "")
    raw_title = it.get("title", "")
    pagemap = it.get("pagemap", {}) or {}
    meta = (pagemap.get("metatags") or [{}])[0]
    posting = (pagemap.get("jobposting") or [{}])[0]

    title, company, location = raw_title, "", ""
    # Common title shapes: "Job Application for X at Company", "Company - X", "X - Company | LinkedIn"
    m = re.match(r"Job Application for (.+?) at (.+)", raw_title)
    if m:
        title, company = m.group(1), m.group(2)
    elif "linkedin.com" in link:
        m = re.match(r"(.+?) hiring (.+?) in (.+?) \|", raw_title)
        if m:
            company, title, location = m.groups()
    elif " - " in raw_title:
        a, b = raw_title.split(" - ", 1)
        if "lever.co" in link or "ashbyhq" in link:
            company, title = a, b
        else:
            title, company = a, b

    if not company:
        m = re.search(r"(?:lever\.co|ashbyhq\.com|greenhouse\.io|workable\.com)/([^/?#]+)", link)
        if m:
            company = m.group(1).replace("-", " ").title()

    title = posting.get("title") or title
    company = posting.get("hiringorganization") or company
    location = posting.get("joblocation") or location or meta.get("og:locale", "")

    return Job(
        title=strip_html(title).replace("...", "").strip(" |-"),
        company=strip_html(company).replace("| LinkedIn", "").strip(" |-"),
        location=strip_html(location),
        url=link,
        source="google",
        description=strip_html(it.get("snippet", "") + " " + meta.get("og:description", "")),
        posted=posting.get("dateposted", ""),
    )


def remotive(role: str, **_) -> list[Job]:
    r = requests.get("https://remotive.com/api/remote-jobs", params={"search": role, "limit": 200},
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [Job(title=j["title"], company=j.get("company_name", ""),
                location=j.get("candidate_required_location", "") + " (Remote)",
                url=j.get("url", ""), source="remotive",
                description=strip_html(j.get("description", ""))[:3000],
                posted=j.get("publication_date", ""), tags=j.get("tags", []),
                job_type=normalize_job_type(j.get("job_type")), remote=True,
                **sal_text(j.get("salary"), "USD"))
            for j in r.json().get("jobs", [])]


def arbeitnow(role: str, pages: int = 3, **_) -> list[Job]:
    out = []
    for page in range(1, pages + 1):
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", params={"page": page},
                         headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            break
        for j in r.json().get("data", []):
            loc = j.get("location", "") + (" (Remote)" if j.get("remote") else "")
            out.append(Job(title=j["title"], company=j.get("company_name", ""), location=loc,
                           url=j.get("url", ""), source="arbeitnow",
                           description=strip_html(j.get("description", ""))[:3000],
                           posted=iso_from_epoch(j.get("created_at")),
                           tags=j.get("tags", []), job_type=normalize_job_type(j.get("job_types")),
                           remote=bool(j.get("remote"))))
    return out


def remoteok(role: str, **_) -> list[Job]:
    r = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [Job(title=j.get("position", ""), company=j.get("company", ""),
                location=(j.get("location") or "Worldwide") + " (Remote)",
                url=j.get("url", ""), source="remoteok",
                description=strip_html(j.get("description", ""))[:3000],
                posted=j.get("date") or "", tags=j.get("tags", []), remote=True,
                **sal(j.get("salary_min"), j.get("salary_max"), "USD"))
            for j in r.json() if isinstance(j, dict) and j.get("position")]


def jobicy(role: str, **_) -> list[Job]:
    r = requests.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": role},
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [Job(title=j.get("jobTitle", ""), company=j.get("companyName", ""),
                location=(j.get("jobGeo") or "Anywhere") + " (Remote)",
                url=j.get("url", ""), source="jobicy",
                description=strip_html(j.get("jobExcerpt", "") + " " + j.get("jobDescription", ""))[:3000],
                posted=j.get("pubDate") or "", level=(j.get("jobLevel") or "").lower()
                if (j.get("jobLevel") or "").lower() in LEVELS else "",
                job_type=normalize_job_type(j.get("jobType")), remote=True,
                **sal(j.get("salaryMin"), j.get("salaryMax"), j.get("salaryCurrency"), j.get("salaryPeriod")))
            for j in r.json().get("jobs", [])]


# --- India-focused sources --------------------------------------------------

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}
GENERIC_ROLE_WORDS = set().union(*ROLE_SYNONYMS[:1]) | {"software", "jobs", "job"}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def instahyre(role: str, exp_range: tuple[int, int] | None = None, job_types: list[str] | None = None,
              pages: int = 3, **_) -> list[Job]:
    """Instahyre public job-search API (India). Filters experience and job type server-side."""
    kinds = [t for t in (job_types or []) if t != "remote"]
    job_type_param = {("full-time",): 1, ("internship",): 2}.get(tuple(kinds))
    out = []
    for page in range(pages):
        params = {"skills": role, "limit": 35, "offset": page * 35}
        if exp_range:
            params["years"] = exp_range[0]
        if job_type_param:
            params["job_type"] = job_type_param
        r = requests.get("https://www.instahyre.com/api/v1/job_search", params=params,
                         headers=BROWSER_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        objs = r.json().get("objects", [])
        for j in objs:
            out.append(Job(title=j.get("title", ""), company=(j.get("employer") or {}).get("company_name", ""),
                           location=(j.get("locations") or "").replace(",", ", ") + ", India",
                           url=j.get("public_url", ""), source="instahyre",
                           description=(j.get("employer") or {}).get("instahyre_note", ""),
                           tags=j.get("keywords") or [],
                           job_type="internship" if job_type_param == 2
                           else normalize_job_type(j.get("title")) or "full-time",
                           remote="work from home" in (j.get("locations") or "").lower()))
        if len(objs) < 35:
            break
    return out


def cutshort(role: str, **_) -> list[Job]:
    """Cutshort (India startups). Pages exist per skill, e.g. /jobs/python-jobs."""
    words = [w for w in slugify(role).split("-") if w not in GENERIC_ROLE_WORDS]
    candidates = [slugify(role)] + words
    for slug in dict.fromkeys(candidates):
        r = requests.get(f"https://cutshort.io/jobs/{slug}-jobs", headers=BROWSER_HEADERS, timeout=TIMEOUT)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        jobs = _find_list(json.loads(m.group(1)), "jobs", "headline") if m else []
        if jobs:
            break
    out = []
    for j in jobs:
        exp = j.get("expRange") or {}
        pay = j.get("salaryRange") or {}
        if pay.get("hideSalary"):  # respect employers who hide the salary
            pay = {}
        out.append(Job(title=j.get("headline", ""), company=(j.get("companyDetails") or {}).get("name", ""),
                       posted=iso_from_epoch(int(j["_id"][:8], 16)) if re.fullmatch(r"[0-9a-f]{24}", j.get("_id", "")) else "",
                       location=", ".join(j.get("locations") or []) + ", India",
                       url=j.get("publicUrl", ""), source="cutshort",
                       description=strip_html(j.get("sanitizedComment", ""))[:3000],
                       exp_min=exp.get("min"), exp_max=exp.get("max"),
                       **sal(pay.get("min"), pay.get("max"), pay.get("currency")),
                       tags=j.get("allSkills") or [],
                       job_type=normalize_job_type(j.get("roleTypes")),
                       remote=j.get("remoteType") in ("remote_only", "remote_okay")))
    return out


def _find_list(obj, key: str, must_have: str) -> list:
    """Depth-first search for obj[...][key] == list of dicts containing `must_have`."""
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict) and must_have in v[0]:
            return v
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return []
    for c in children:
        found = _find_list(c, key, must_have)
        if found:
            return found
    return []


def internshala(role: str, job_types: list[str] | None = None, pages: int = 2, **_) -> list[Job]:
    """Internshala (India, freshers to ~5 yrs). Jobs always; internships too when that type is selected."""
    kinds = [t for t in (job_types or []) if t != "remote"]
    out = []
    if not kinds or "full-time" in kinds:
        out += _internshala_listing(role, "jobs", "jobs", pages)
    if "internship" in kinds:
        out += _internshala_listing(role, "internships", "internship", pages)
    return out


def _internshala_listing(role: str, section: str, suffix: str, pages: int) -> list[Job]:
    """Scrape /jobs/<slug>-jobs/ or /internships/<slug>-internship/. Category slugs only exist for
    common roles/skills, so fall back from 'python-developer' to 'python', then to keyword search."""
    from bs4 import BeautifulSoup

    kind = "internship" if section == "internships" else "job"
    words = [w for w in slugify(role).split("-") if w not in GENERIC_ROLE_WORDS]
    paths = [f"{slug}-{suffix}" for slug in dict.fromkeys([slugify(role)] + words)]
    paths.append(f"keywords-{slugify(role)}")
    for path in paths:
        out = []
        for page in range(1, pages + 1):
            url = f"https://internshala.com/{section}/{path}/" + (f"page-{page}/" if page > 1 else "")
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
            if r.status_code != 200 or r.url.rstrip("/").endswith(section):  # unknown slug -> redirected
                break
            cards = BeautifulSoup(r.text, "html.parser").select("div.individual_internship[internshipid]")
            # keyword search mixes jobs and internships; keep the kind this listing is for
            tags = [] if path.startswith("keywords-") else path.rsplit("-", 1)[0].split("-")
            out += [_internshala_card(c, tags) for c in cards if c.get("employment_type", kind) == kind]
            if not cards:
                break
        if out := [j for j in out if j]:
            return out
    return []


def _internshala_card(c, tags: list[str]) -> Job | None:
    a = c.select_one("a.job-title-href")
    if not a:
        return None
    internship = c.get("employment_type") == "internship"
    locs = ", ".join(x.get_text(strip=True) for x in c.select(".locations a")) or "India"
    exp_text = pay_text = ""
    status = c.select_one("[class*=status]")
    posted = iso_from_relative(status.get_text(" ", strip=True)) if status else ""
    for item in c.select(".row-1-item"):
        if item.select_one(".ic-16-briefcase"):
            exp_text = item.get_text(" ", strip=True)
        if item.select_one(".ic-16-money"):
            pay_text = (item.select_one("span") or item).get_text(" ", strip=True)
    m = re.search(r"(\d+)", exp_text)
    exp_min = int(m.group(1)) if m else (0 if "fresher" in exp_text.lower() or internship else None)
    return Job(title=a.get_text(strip=True),
               company=(c.select_one(".company-name") or a).get_text(strip=True),
               location=locs + ("" if "india" in locs.lower() else ", India"),
               url="https://internshala.com" + a["href"], source="internshala", posted=posted,
               description=(c.select_one(".about_job .text") or a).get_text(" ", strip=True)[:3000],
               exp_min=exp_min, exp_max=None if exp_min is None else 99,
               level="junior" if exp_min == 0 else "",
               tags=tags,  # the category page it was listed under, e.g. ["python"]
               job_type="internship" if internship else "full-time",
               remote="work from home" in locs.lower(),
               **sal_text(pay_text if "/" in pay_text else pay_text + " /year", "INR"))


def himalayas(role: str, locations: list[str] | None = None, **_) -> list[Job]:
    """Himalayas remote jobs. Restricted to India-eligible jobs when the location is in India."""
    params = {"q": role}
    if locations and is_india(locations):
        params["country"] = "India"
    out = []
    for page in (1, 2):
        r = requests.get("https://himalayas.app/jobs/api/search", params={**params, "page": page},
                         headers=BROWSER_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        jobs = r.json().get("jobs", [])
        for j in jobs:
            seniority = [s.lower() for s in (j.get("seniority") or [])]
            level = next((s for s in ("intern", "entry", "junior", "mid", "senior", "lead", "staff", "principal")
                          if any(s in x for x in seniority)), "")
            restr = j.get("locationRestrictions") or []
            out.append(Job(title=j.get("title", ""), company=j.get("companyName", ""),
                           location=(", ".join(restr) or "Worldwide") + " (Remote)",
                           url=j.get("applicationLink") or j.get("guid", ""), source="himalayas",
                           description=strip_html(j.get("excerpt", "") + " " + j.get("description", ""))[:3000],
                           posted=iso_from_epoch(j.get("pubDate")),
                           level=level, tags=j.get("categories") or [], remote=True,
                           job_type=normalize_job_type(j.get("employmentType")),
                           **sal(j.get("minSalary"), j.get("maxSalary"), j.get("currency"), j.get("salaryPeriod"))))
        if len(jobs) < 20:
            break
    return out


ADZUNA_CURRENCY = {"in": "INR", "gb": "GBP", "us": "USD", "ca": "CAD", "au": "AUD", "nz": "NZD", "sg": "SGD",
                   "za": "ZAR", "br": "BRL", "mx": "MXN", "pl": "PLN", "ch": "CHF", "de": "EUR", "fr": "EUR",
                   "nl": "EUR", "at": "EUR", "it": "EUR", "es": "EUR", "be": "EUR"}


def adzuna(role: str, locations: list[str] | None = None, days: float | None = None, **_) -> list[Job]:
    """Adzuna API (free key: developer.adzuna.com). Country 'in' when location is Indian, else ADZUNA_COUNTRY.
    Newest first: by relevance Adzuna returns many postings years old."""
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        print("[adzuna] ADZUNA_APP_ID / ADZUNA_APP_KEY not set - skipping.", file=sys.stderr)
        return []
    if not quota.allow("adzuna", cost=2):  # up to 2 pages fetched below
        print("[adzuna] daily quota used up - skipping.", file=sys.stderr)
        return []
    locations = locations or []
    country = "in" if is_india(locations) else os.getenv("ADZUNA_COUNTRY", "in")
    where = next((l for l in locations if l.lower() not in ("india", "remote")), "")
    params = {"app_id": app_id, "app_key": app_key, "title_only": role, "where": where,  # role in the title
              "results_per_page": 50, "sort_by": "date", "content-type": "application/json"}
    if days:
        params["max_days_old"] = max(1, int(days))
    out = []
    for page in (1, 2):
        r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}", params=params,
                         timeout=TIMEOUT)
        r.raise_for_status()
        results = r.json().get("results", [])
        for j in results:
            out.append(Job(title=strip_html(j.get("title", "")),
                           company=(j.get("company") or {}).get("display_name", ""),
                           location=(j.get("location") or {}).get("display_name", ""),
                           url=j.get("redirect_url", ""), source="adzuna",
                           description=strip_html(j.get("description", "")),
                           posted=j.get("created") or "",
                           job_type=normalize_job_type(j.get("contract_time"), j.get("contract_type")),
                           **sal(j.get("salary_min"), j.get("salary_max"), ADZUNA_CURRENCY.get(country))))
        if len(results) < 50:
            break
    return out


def jooble(role: str, locations: list[str] | None = None, **_) -> list[Job]:
    """Jooble API (free key: jooble.org/api/about). Covers India and ~70 other countries."""
    key = os.getenv("JOOBLE_API_KEY")
    if not key:
        print("[jooble] JOOBLE_API_KEY not set - skipping.", file=sys.stderr)
        return []
    locs = [l for l in (locations or []) if l.lower() != "remote"] or [""]
    if not quota.allow("jooble", cost=len(locs[:3])):  # one call per location below
        print("[jooble] daily quota used up - skipping.", file=sys.stderr)
        return []
    out = []
    for loc in locs[:3]:
        r = requests.post(f"https://jooble.org/api/{key}", json={"keywords": role, "location": loc},
                          timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            out.append(Job(title=strip_html(j.get("title", "")), company=j.get("company", ""),
                           location=j.get("location", ""), url=j.get("link", ""), source="jooble",
                           description=strip_html(j.get("snippet", "")),
                           posted=j.get("updated") or "", job_type=normalize_job_type(j.get("type")),
                           **sal_text(j.get("salary"), "INR" if is_india(locs) else "")))
    return out


JOBVETTA_MCP = "https://api.jobvetta.com/mcp"
_jobvetta_cache: dict[tuple, tuple[float, list[Job]]] = {}
JOBVETTA_CACHE_TTL = 30 * 60  # free key = 50 calls per UTC day, so never repeat a search within 30 min


def _mcp_call(url: str, key: str, tool: str, arguments: dict) -> dict:
    """Call one tool on a streamable-HTTP MCP server (stateless: no session needed) and return its result."""
    r = requests.post(url, timeout=TIMEOUT, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                  "params": {"name": tool, "arguments": arguments}},
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"})
    if r.status_code == 429:
        raise RuntimeError("daily limit reached (50 calls per UTC day on the free key)")
    body = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):  # SSE: take the last data line
        body = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")][-1]
    msg = json.loads(body)
    if "error" in msg:
        raise RuntimeError(msg["error"].get("message", msg["error"]))
    result = msg.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(" ".join(c.get("text", "") for c in result.get("content", []))[:200])
    return result


def jobvetta(role: str, locations: list[str] | None = None, days: float | None = None, **_) -> list[Job]:
    """Jobvetta (India jobs checked against employers' own sites), through its hosted MCP server.
    Free key: jobvetta.com, 50 calls per UTC day. One call per search, up to 10 jobs."""
    key = os.getenv("JOBVETTA_API_KEY")
    if not key:
        print("[jobvetta] JOBVETTA_API_KEY not set - skipping.", file=sys.stderr)
        return []
    locations = locations or []
    if locations and not is_india(locations):
        return []  # India-only data: don't spend a call on e.g. "berlin"
    city = next((l for l in locations if l.lower().strip() in INDIA_CITIES and l.lower().strip() != "india"), "")
    args = {"q": role, "limit": 10}
    if city:
        args["location"] = city
    if days:
        args["days"] = max(1, int(days))
    cache_key = tuple(sorted(args.items()))
    hit = _jobvetta_cache.get(cache_key)
    if hit and time.time() - hit[0] < JOBVETTA_CACHE_TTL:
        return hit[1]
    if not quota.allow("jobvetta"):
        print("[jobvetta] daily quota used up - skipping.", file=sys.stderr)
        return []

    result = _mcp_call(JOBVETTA_MCP, key, "search_jobs", args)
    data = result.get("structuredContent")
    if data is None:  # older servers put the JSON in a text block
        text = " ".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        try:
            data = json.loads(text)
        except ValueError:
            print(f"[jobvetta] unexpected reply format: {text[:200]}", file=sys.stderr)
            return []
    rows = data.get("jobs", []) if isinstance(data, dict) else data
    out = []
    for j in rows or []:
        pay = j.get("salary")
        pay_kw = (sal(pay.get("min"), pay.get("max"), pay.get("currency") or "INR", pay.get("period"))
                  if isinstance(pay, dict) else sal_text(str(pay), "INR") if pay else {})
        out.append(Job(title=j.get("title", ""), company=j.get("company", ""),
                       location=(j.get("location") or "India") + ("" if "india" in (j.get("location") or "").lower()
                                                                 else ", India"),
                       url=j.get("url", ""), source="jobvetta",
                       job_type=normalize_job_type(j.get("employment_type")),
                       remote="remote" in (j.get("work_model") or "").lower(), **pay_kw))
    _jobvetta_cache[cache_key] = (time.time(), out)
    return out


def company_careers(role: str, **_) -> list[Job]:
    """Jobs straight from company careers pages (Greenhouse / Lever / Ashby / SmartRecruiters boards
    of the companies in companies.json). See careers.py."""
    import careers

    # While the web app is still warming up, use the boards loaded so far instead of starting a second
    # download next to it (that overloads the connection and makes boards fail).
    rows = careers.cached_jobs() if careers.status()["loading"] else careers.fetch_all()
    out = []
    for j in rows:
        job = Job(title=j["title"], company=j["company"], location=j["location"], url=j["url"],
                  source="careers", description=j["description"],
                  # Workday gives "Posted 3 Days Ago" instead of a date
                  posted=iso_from_relative(j["posted"]) if j["posted"].startswith("Posted") else j["posted"],
                  tags=[j["department"]] if j["department"] else [],
                  job_type=normalize_job_type(j["job_type"]), remote=j["remote"],
                  **(sal(*j["salary"]) if j["salary"] else {}))
        if matches_role(job, role):  # cheap pre-filter: boards hold ~16k jobs
            out.append(job)
    return out


SOURCES = {
    "google": None,
    "careers": company_careers,
    # India
    "instahyre": instahyre, "cutshort": cutshort, "internshala": internshala,
    "adzuna": adzuna, "jooble": jooble, "jobvetta": jobvetta,
    # Global / remote
    "himalayas": himalayas, "remotive": remotive, "arbeitnow": arbeitnow, "remoteok": remoteok, "jobicy": jobicy,
}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def search_jobs(role: str, experience: str | None = None, location: str | None = None,
                sources: list[str] | None = None, max_google: int = 50,
                country_code: str | None = None, date_restrict: str | None = None,
                strict: bool = False, min_salary: str | float | None = None,
                max_salary: str | float | None = None, currency: str | None = None,
                salary_only: bool = False, job_types: list[str] | None = None) -> list[Job]:
    """min/max_salary are annual amounts in `currency` ('12 LPA', '80k', 1200000 all work).
    currency defaults to INR for Indian locations, USD otherwise.
    date_restrict ('d1', 'd7', 'w2', 'm1', 'm3') limits every source by posted date, not just Google."""
    sources = sources or list(SOURCES)
    exp_range = parse_experience_arg(experience)
    locations = [l.strip() for l in (location or "").split(",") if l.strip()]
    currency = (currency or ("INR" if is_india(locations) else "USD")).upper()
    min_sal, max_sal = parse_amount(min_salary), parse_amount(max_salary)
    job_types = [t.lower() for t in (job_types or [])]
    days = window_days(date_restrict)
    if bad := [t for t in job_types if t not in JOB_TYPES]:
        raise ValueError(f"Unknown job type {bad}. Use: {', '.join(JOB_TYPES)}")

    metered = {"google", "adzuna", "jooble", "jobvetta"}
    if any(s in metered for s in sources) and not quota.metered_sources_allowed():
        print(f"[quota] app-wide search rate exceeded - skipping metered sources this search: "
              f"{sorted(set(sources) & metered)}", file=sys.stderr)
        sources = [s for s in sources if s not in metered]

    def fetch(name: str) -> list[Job]:
        try:
            if name == "google":
                got = google_search(role, experience, locations, max_google, country_code, date_restrict)
            else:
                got = SOURCES[name](role, exp_range=exp_range, locations=locations, job_types=job_types,
                                    days=days)
            print(f"[{name}] fetched {len(got)}", file=sys.stderr)
            stats.record(name, len(got))
            return got
        except Exception as e:  # one broken source shouldn't kill the run
            print(f"[{name}] failed: {e}", file=sys.stderr)
            stats.record(name, None, error=str(e))
            return []

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        raw = [job for batch in pool.map(fetch, sources) for job in batch]

    seen, results = set(), []
    for job in raw:
        keys = {job.url, (job.title.lower(), job.company.lower(), job.location.lower())}
        if fkey := _fuzzy_key(job):
            keys.add(fkey)
        keys -= {""}
        if keys & seen:
            continue
        seen |= keys
        # cheap checks first; the enrichment below runs regexes over long descriptions
        if not ((matches_role(job, role) or job.source == "google")
                and matches_location(job, locations) and matches_posted(job, days)):
            continue
        enrich_experience(job)
        enrich_salary(job, "INR" if is_india([job.location.split(",")[-1]]) else "")
        enrich_job_type(job)
        if (matches_experience(job, exp_range, strict)
                and matches_salary(job, min_sal, max_sal, currency, salary_only)
                and matches_job_type(job, job_types)):
            results.append(job)
    if min_sal or max_sal:  # jobs with a known salary first
        results.sort(key=lambda j: j.salary_min is None and j.salary_max is None)
    return results


SORTS = {"relevance": "Relevance", "salary_desc": "Salary: high to low", "salary_asc": "Salary: low to high"}


def sort_jobs(jobs: list[Job], sort: str | None, currency: str = "USD") -> list[Job]:
    """Salary sorts compare across currencies (converted to `currency`); high-to-low ranks by the top of
    each range, low-to-high by the bottom. Jobs without a salary always go last."""
    if sort not in ("salary_desc", "salary_asc"):
        return jobs
    desc = sort == "salary_desc"

    def key(j: Job):
        amount = (j.salary_max if desc else j.salary_min)
        if amount is None:
            amount = j.salary_min if desc else j.salary_max
        value = convert(amount, j.salary_currency, currency)
        if value is None:
            return (1, 0)
        return (0, -value if desc else value)

    return sorted(jobs, key=key)


def write_csv(jobs: list[Job], f) -> None:
    fields = [k for k in Job.__dataclass_fields__ if k != "description"]
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for j in jobs:
        row = asdict(j)
        row["tags"] = ", ".join(row["tags"])
        w.writerow(row)


def save(jobs: list[Job], path: str) -> None:
    if path.lower().endswith(".json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump([asdict(j) for j in jobs], f, indent=2, ensure_ascii=False)
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
            write_csv(jobs, f)
    print(f"Saved {len(jobs)} jobs to {path}", file=sys.stderr)


def fmt_exp(j: Job) -> str:
    if j.exp_min is not None:
        return f"{j.exp_min}+ yrs" if (j.exp_max or 99) >= 99 else f"{j.exp_min}-{j.exp_max} yrs"
    return j.level or "-"


def fmt_type(j: Job) -> str:
    """'Full-time · Remote', 'Internship', '-'."""
    parts = [t.strip().capitalize() for t in j.job_type.split(",") if t.strip()]
    if j.remote:
        parts.append("Remote")
    return " · ".join(parts) or "-"


def print_table(jobs: list[Job]) -> None:
    def cut(s, n):
        return s if len(s) <= n else s[: n - 1] + "…"
    print(f"{'TITLE':42} {'COMPANY':20} {'LOCATION':24} {'EXP':9} {'SALARY/YR':16} {'TYPE':18} {'POSTED':10} SOURCE")
    print("-" * 161)
    for j in jobs:
        print(f"{cut(j.title,42):42} {cut(j.company,20):20} {cut(j.location,24):24} {fmt_exp(j):9} "
              f"{cut(fmt_salary(j),16):16} {cut(fmt_type(j),18):18} {fmt_posted(j):10} {j.source}")
        print(f"    {j.url}")


def main() -> None:
    p = argparse.ArgumentParser(description="Worldwide job scraper (Google Custom Search + free job APIs)")
    p.add_argument("--role", required=True, help='Job role, e.g. "python developer"')
    p.add_argument("--experience", help="Years (3-5, 5+, 2) or level: " + ", ".join(LEVELS))
    p.add_argument("--location", help='Comma-separated, e.g. "india,germany,remote"')
    p.add_argument("--sources", default=",".join(SOURCES), help=f"Comma list of: {', '.join(SOURCES)}")
    p.add_argument("--max-google", type=int, default=50, help="Max Google results (10 per API call)")
    p.add_argument("--country-code", help="Google 'gl' country boost, e.g. in, us, de")
    p.add_argument("--date", default="", help="Posted within: d1 (24 hours), d7, w2, m1, m3 (default: any time). "
                                             "Applies to every source; undated jobs are dropped")
    p.add_argument("--strict", action="store_true", help="Drop jobs with no detectable experience info")
    p.add_argument("--min-salary", help='Min annual salary, e.g. 1200000, "12 LPA", 12L, 80k')
    p.add_argument("--max-salary", help="Max annual salary (same formats)")
    p.add_argument("--currency", help="Currency of the salary filter: INR, USD, EUR, GBP ... "
                                      "(default INR for Indian locations, else USD)")
    p.add_argument("--salary-only", action="store_true", help="Drop jobs that don't list a salary")
    p.add_argument("--job-type", help=f"Comma list of: {', '.join(JOB_TYPES)}. full-time/internship are "
                                      "alternatives; remote is combined with them, e.g. 'internship,remote'")
    p.add_argument("--sort", choices=list(SORTS), default="relevance",
                   help="salary_desc / salary_asc compare across currencies; jobs without salary go last")
    p.add_argument("--out", help="Save to .csv or .json")
    a = p.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        stream.reconfigure(encoding="utf-8", errors="replace")

    job_types = [t.strip().lower() for t in (a.job_type or "").split(",") if t.strip()]
    try:
        parse_experience_arg(a.experience), parse_amount(a.min_salary), parse_amount(a.max_salary)
        if bad := [t for t in job_types if t not in JOB_TYPES]:
            raise ValueError(f"Unknown job type {bad}. Use: {', '.join(JOB_TYPES)}")
    except ValueError as e:
        p.error(str(e))
    jobs = search_jobs(a.role, a.experience, a.location, [s.strip() for s in a.sources.split(",")],
                       a.max_google, a.country_code, a.date or None, a.strict,
                       a.min_salary, a.max_salary, a.currency, a.salary_only, job_types)
    locations = (a.location or "").split(",")
    jobs = sort_jobs(jobs, a.sort, (a.currency or ("INR" if is_india(locations) else "USD")).upper())
    print_table(jobs)
    print(f"\n{len(jobs)} matching jobs", file=sys.stderr)
    if a.out:
        save(jobs, a.out)


if __name__ == "__main__":
    main()
