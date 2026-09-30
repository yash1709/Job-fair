"""Finds newly-opened company job boards among two candidate pools and adds them to companies.json -
the "keep growing over time" half of the careers source. companies.json itself is a frozen list until
this runs: a company with no jobs today might open a board next month, and without this script the app
would never notice, since it only re-fetches boards already on the list.

Candidate pools:
  - YC's full company directory (yc_candidates.json) - startups are heavy adopters of the exact ATS
    platforms this app already reads, so this pool has a good hit rate.
  - All NSE/BSE-listed companies (nse_candidates.json, sourced from 5paisa's stock listing since that's
    the full universe of Indian public companies) - a much bigger, much lower-hit-rate pool, since most
    large listed Indian corporates use in-house or enterprise HR systems this app doesn't read. Still
    worth checking: a real (if smaller) fraction do use Greenhouse/Lever/Ashby/SmartRecruiters.

This is deliberately NOT part of the live app. Checking thousands of candidates against several ATS
platforms takes 1-2+ hours - fine for a periodic background job, not something to run inside the app's
hourly refresh loop (or on every visitor's request). Run this from a schedule instead: see
.github/workflows/discover-companies.yml, which runs it weekly and commits anything new.

Usage:
    python discover.py                       # check all untracked candidates in both pools
    python discover.py --refresh-candidates  # also re-fetch both candidate lists from source first
    python discover.py --sources yc          # only check the YC pool (or --sources nse)
    python discover.py --limit 500           # check only the first N untracked candidates per pool
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import careers

CANDIDATES_FILE = Path(__file__).with_name("yc_candidates.json")
EXCLUDED_FILE = Path(__file__).with_name("yc_excluded.json")
REVIEW_FILE = Path(__file__).with_name("yc_needs_review.json")
PRUNED_FILE = Path(__file__).with_name("pruned_companies.json")  # companies prune.py removed for having no jobs

NSE_CANDIDATES_FILE = Path(__file__).with_name("nse_candidates.json")
NSE_EXCLUDED_FILE = Path(__file__).with_name("nse_excluded.json")
NSE_REVIEW_FILE = Path(__file__).with_name("nse_needs_review.json")
NSE_NAMES_FILE = Path(__file__).with_name("nse_names.json")  # ticker -> official company name (from NSE's own list)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
T = 10
# Short, common-word slugs are where real collisions happened before (a completely unrelated company
# happens to use the same literal slug on the same ATS) - see yc_excluded.json for confirmed examples
# (bird, stage, pulse, ...). Below this length, a hit is flagged for review instead of auto-added.
MIN_SAFE_SLUG_LENGTH = 5
# A "hit" with no posting in this many days is almost certainly an abandoned board, not a live one - e.g.
# probing NSE ticker "aaradhya" found a single SmartRecruiters posting from 2022 for an unrelated small
# Kolkata textile company. Generous on purpose: this only needs to rule out years-old dead accounts.
MAX_HIT_AGE_DAYS = 400


def refresh_candidates() -> list[str]:
    r = requests.get("https://www.ycombinator.com/companies/sitemap", headers=UA, timeout=30)
    r.raise_for_status()
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    non_company = [l for l in locs if re.search(r"/companies/(industry|batch|location|tag|founders)", l)]
    slugs = sorted({l.rsplit("/", 1)[1] for l in locs if l not in non_company
                   and re.match(r"https://www\.ycombinator\.com/companies/[^/]+$", l)})
    CANDIDATES_FILE.write_text(json.dumps(slugs, indent=1), encoding="utf-8")
    print(f"[discover] refreshed candidate list: {len(slugs)} YC companies", flush=True)
    return slugs


def refresh_nse_candidates() -> list[str]:
    """All NSE/BSE-listed companies, from 5paisa's public sitemap of per-stock pages (the same universe as
    https://www.5paisa.com/stocks/all, which loads results a page at a time via a "Load more" button and
    has no plain page-number/JSON API to fetch in bulk - the sitemap has every page's URL up front instead).
    Also pulls NSE's own official symbol -> company name list, since 5paisa's per-stock pages only show a
    short display name ("Reliance"), not the full legal name ("Reliance Industries Limited")."""
    r = requests.get("https://www.5paisa.com/stocks-sitemap.xml", headers=UA, timeout=30)
    r.raise_for_status()
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    slugs = sorted({m.group(1) for l in locs if (m := re.search(r"/stocks/([a-z0-9-]+)-share-price$", l))})
    NSE_CANDIDATES_FILE.write_text(json.dumps(slugs, indent=1), encoding="utf-8")
    print(f"[discover] refreshed NSE/BSE candidate list: {len(slugs)} listed companies", flush=True)

    try:
        r = requests.get("https://archives.nseindia.com/content/equities/EQUITY_L.csv", headers=UA, timeout=30)
        r.raise_for_status()
        names = {row["SYMBOL"].strip().lower(): row["NAME OF COMPANY"].strip()
                for row in csv.DictReader(io.StringIO(r.text)) if row.get("SYMBOL")}
        NSE_NAMES_FILE.write_text(json.dumps(names, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"[discover] refreshed {len(names)} official NSE company names", flush=True)
    except Exception as e:
        print(f"[discover] couldn't refresh NSE official names, will fall back to slug-derived names "
             f"for unmatched ones: {e!s:.150}", flush=True)

    return slugs


def gh(s):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", headers=UA, timeout=T)
    return len(r.json().get("jobs", [])) if r.status_code == 200 else 0


def lever(s):
    r = requests.get(f"https://api.lever.co/v0/postings/{s}", params={"mode": "json"}, headers=UA, timeout=T)
    return len(r.json()) if r.status_code == 200 and isinstance(r.json(), list) else 0


def ashby(s):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{s}", headers=UA, timeout=T)
    return len(r.json().get("jobs", [])) if r.status_code == 200 else 0


def smartrecruiters(s):
    r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings",
                     params={"limit": 1}, headers=UA, timeout=T)
    if r.status_code != 200:
        return 0
    data = r.json()
    return data.get("totalFound") or len(data.get("content", []))


ATS = {"greenhouse": gh, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters}


def probe_one(task: tuple[str, str]) -> tuple[str, str, int]:
    slug, ats = task
    fn = ATS[ats]
    for attempt in range(2):
        try:
            return slug, ats, fn(slug)
        except (requests.ConnectionError, requests.Timeout):
            continue
        except Exception:
            return slug, ats, 0
    return slug, ats, 0


def yc_name(slug: str) -> str | None:
    for attempt in range(2):
        try:
            r = requests.get(f"https://www.ycombinator.com/companies/{slug}", headers=UA, timeout=T)
            m = re.search(r"<title>([^<]+)</title>", r.text)
            if m:
                name = re.split(r"\s*[:|]\s*", m.group(1))[0].strip()
                if name and name.lower() != "y combinator":
                    return name
            return None
        except Exception:
            continue
    return None


_nse_names_cache: dict | None = None


def nse_name(slug: str) -> str | None:
    """Looks up the official company name for an NSE ticker slug. Falls back to None (caller then
    title-cases the raw slug instead) for BSE-only tickers not in NSE's own list, or if it's missing."""
    global _nse_names_cache
    if _nse_names_cache is None:
        try:
            _nse_names_cache = json.loads(NSE_NAMES_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _nse_names_cache = {}
    return _nse_names_cache.get(slug.lower())


def _recently_posted(ats: str, slug: str, within_days: int = MAX_HIT_AGE_DAYS) -> bool:
    """Only called for an actual hit (never for the bulk probe), so this extra full-board fetch doesn't
    multiply the cost of checking thousands of candidates. Requires at least one job posted within
    `within_days` - otherwise the "hit" is just a long-abandoned board, not evidence of live hiring."""
    try:
        jobs = careers.fetch_board({"ats": ats, "slug": slug, "name": slug})
    except Exception:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=within_days)
    for j in jobs:
        raw = (j.get("posted") or "").strip()
        if not raw:
            continue
        if re.fullmatch(r"\d{10,13}", raw):
            raw = datetime.fromtimestamp(int(raw) / (1000 if len(raw) > 10 else 1), tz=timezone.utc).isoformat()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            continue
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            return True
    return False


def discover_pool(label: str, candidates: list[str], excluded, existing: list[dict],
                  name_resolver, workers: int, limit: int | None) -> tuple[list[dict], dict]:
    """Checks one candidate pool against every ATS in ATS, returning (accepted, review) - accepted
    companies still need to be appended to companies.json and saved by the caller."""
    tracked_slugs = {c["slug"].lower() for c in existing}
    tracked_names = {re.sub(r"[^a-z0-9]", "", c["name"].lower()) for c in existing}

    def already_tracked(slug: str) -> bool:
        return slug.lower() in tracked_slugs or re.sub(r"[^a-z0-9]", "", slug.lower()) in tracked_names

    todo = [s for s in candidates if s not in excluded and not already_tracked(s)]
    if limit:
        todo = todo[:limit]
    print(f"[discover:{label}] {len(todo)} of {len(candidates)} companies not yet tracked - checking them",
         flush=True)
    if not todo:
        print(f"[discover:{label}] nothing to check", flush=True)
        return [], {}

    tasks = [(s, a) for s in todo for a in ATS]
    hits: dict[str, tuple[str, int]] = {}  # slug -> (ats, jobs), keeping the best ats per slug
    done = 0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(probe_one, t): t for t in tasks}
        for fut in as_completed(futures):
            slug, ats, n = fut.result()
            done += 1
            if n and (slug not in hits or n > hits[slug][1]):
                hits[slug] = (ats, n)
                print(f"HIT [{label}] {slug} | {ats} | {n} jobs", flush=True)
            if done % 1000 == 0:
                print(f"... [{label}] {done}/{len(tasks)} requests done, {len(hits)} hits so far", flush=True)

    accepted, review = [], {}
    for slug, (ats, n) in hits.items():
        if len(slug) < MIN_SAFE_SLUG_LENGTH:
            review[slug] = {"ats": ats, "jobs": n, "reason": f"slug shorter than {MIN_SAFE_SLUG_LENGTH} chars - "
                                                             "higher risk of being an unrelated company's board"}
            continue
        if not _recently_posted(ats, slug):
            print(f"SKIP [{label}] {slug} | {ats} | {n} jobs, but nothing posted in the last "
                 f"{MAX_HIT_AGE_DAYS} days - likely an abandoned board", flush=True)
            continue
        name = name_resolver(slug) or slug.replace("-", " ").title()
        accepted.append({"name": name, "ats": ats, "slug": slug})
    return accepted, review


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh-candidates", action="store_true", help="re-fetch both candidate lists first")
    ap.add_argument("--sources", default="yc,nse", help="comma list of pools to check: yc, nse")
    ap.add_argument("--limit", type=int, default=None, help="check only the first N untracked candidates per pool")
    ap.add_argument("--workers", type=int, default=20, help="parallel HTTP requests")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sources = {s.strip().lower() for s in args.sources.split(",") if s.strip()}

    existing = careers.load_companies()

    # Recheck previously-pruned companies first: unlike fresh candidates (unknown ats/slug, needs a
    # multi-way guess), these already have a known board, so it's one direct check each, not several.
    pruned = json.loads(PRUNED_FILE.read_text(encoding="utf-8")) if PRUNED_FILE.exists() else []
    if pruned:
        print(f"[discover] rechecking {len(pruned)} previously-pruned companies", flush=True)

        def recheck(c: dict) -> tuple[dict, int]:
            try:
                return c, len(careers.fetch_board(c))
            except Exception as e:
                print(f"[discover] recheck of {c['name']} failed: {e!s:.100}", flush=True)
                return c, 0

        with ThreadPoolExecutor(min(args.workers, len(pruned))) as pool:
            counts = list(pool.map(recheck, pruned))
        revived = [c for c, n in counts if n > 0]
        if revived:
            existing = existing + revived
            careers.save_companies(existing)
            pruned = [c for c in pruned if c not in revived]
            PRUNED_FILE.write_text(json.dumps(pruned, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"[discover] {len(revived)} pruned companies are hiring again - re-added:", flush=True)
            for c in revived:
                print(f"   + {c['name']} ({c['ats']}/{c['slug']})", flush=True)

    pools = []
    if "yc" in sources:
        pools.append(dict(label="YC", candidates_file=CANDIDATES_FILE, excluded_file=EXCLUDED_FILE,
                          review_file=REVIEW_FILE, refresh=refresh_candidates, name_resolver=yc_name))
    if "nse" in sources:
        pools.append(dict(label="NSE", candidates_file=NSE_CANDIDATES_FILE, excluded_file=NSE_EXCLUDED_FILE,
                          review_file=NSE_REVIEW_FILE, refresh=refresh_nse_candidates, name_resolver=nse_name))

    for pool in pools:
        candidates = pool["refresh"]() if args.refresh_candidates else json.loads(
            pool["candidates_file"].read_text(encoding="utf-8"))
        excluded = json.loads(pool["excluded_file"].read_text(encoding="utf-8")) if pool["excluded_file"].exists() \
            else {}

        accepted, review = discover_pool(pool["label"], candidates, excluded, existing,
                                         pool["name_resolver"], args.workers, args.limit)
        if accepted:
            existing = existing + accepted
            careers.save_companies(existing)
            print(f"[discover:{pool['label']}] added {len(accepted)} newly-found companies to companies.json:",
                 flush=True)
            for c in accepted:
                print(f"   + {c['name']} ({c['ats']}/{c['slug']})", flush=True)
        else:
            print(f"[discover:{pool['label']}] no new companies to add this run", flush=True)

        pool["review_file"].write_text(json.dumps(review, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        if review:
            print(f"[discover:{pool['label']}] {len(review)} short-slug hits need manual review - "
                 f"see {pool['review_file'].name}", flush=True)


if __name__ == "__main__":
    main()
