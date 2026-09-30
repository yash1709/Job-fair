"""Finds newly-opened company job boards among YC's full company directory and adds them to
companies.json - the "keep growing over time" half of the careers source. companies.json itself is
a frozen list until this runs: a YC company with no jobs today might open a board next month, and
without this script the app would never notice, since it only re-fetches boards already on the list.

This is deliberately NOT part of the live app. Checking thousands of candidates against Greenhouse/
Lever/Ashby takes 1-2+ hours - fine for a periodic background job, not something to run inside the
app's hourly refresh loop (or on every visitor's request). Run this from a schedule instead:
see .github/workflows/discover-companies.yml, which runs it weekly and commits anything new.

Usage:
    python discover.py                       # check all untracked YC candidates, update companies.json
    python discover.py --refresh-candidates  # also re-fetch yc_candidates.json from YC's sitemap first
    python discover.py --limit 500           # check only the first N untracked candidates (for testing)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

import careers

CANDIDATES_FILE = Path(__file__).with_name("yc_candidates.json")
EXCLUDED_FILE = Path(__file__).with_name("yc_excluded.json")
REVIEW_FILE = Path(__file__).with_name("yc_needs_review.json")
PRUNED_FILE = Path(__file__).with_name("pruned_companies.json")  # companies prune.py removed for having no jobs

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
T = 10
# Short, common-word slugs are where real collisions happened before (a completely unrelated company
# happens to use the same literal slug on the same ATS) - see yc_excluded.json for confirmed examples
# (bird, stage, pulse, ...). Below this length, a hit is flagged for review instead of auto-added.
MIN_SAFE_SLUG_LENGTH = 5


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


def gh(s):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", headers=UA, timeout=T)
    return len(r.json().get("jobs", [])) if r.status_code == 200 else 0


def lever(s):
    r = requests.get(f"https://api.lever.co/v0/postings/{s}", params={"mode": "json"}, headers=UA, timeout=T)
    return len(r.json()) if r.status_code == 200 and isinstance(r.json(), list) else 0


def ashby(s):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{s}", headers=UA, timeout=T)
    return len(r.json().get("jobs", [])) if r.status_code == 200 else 0


ATS = {"greenhouse": gh, "lever": lever, "ashby": ashby}


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh-candidates", action="store_true", help="re-fetch yc_candidates.json from YC first")
    ap.add_argument("--limit", type=int, default=None, help="check only the first N untracked candidates")
    ap.add_argument("--workers", type=int, default=20, help="parallel HTTP requests")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    candidates = refresh_candidates() if args.refresh_candidates else json.loads(
        CANDIDATES_FILE.read_text(encoding="utf-8"))
    excluded = json.loads(EXCLUDED_FILE.read_text(encoding="utf-8")) if EXCLUDED_FILE.exists() else {}

    existing = careers.load_companies()

    # Recheck previously-pruned companies first: unlike YC candidates (unknown ats/slug, needs a 3-way
    # guess), these already have a known board, so it's one direct check each, not three.
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

    tracked_slugs = {c["slug"].lower() for c in existing}
    tracked_names = {re.sub(r"[^a-z0-9]", "", c["name"].lower()) for c in existing}

    def already_tracked(slug: str) -> bool:
        return slug.lower() in tracked_slugs or re.sub(r"[^a-z0-9]", "", slug.lower()) in tracked_names

    todo = [s for s in candidates if s not in excluded and not already_tracked(s)]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[discover] {len(todo)} of {len(candidates)} YC companies not yet tracked - checking them", flush=True)
    if not todo:
        print("[discover] nothing to check", flush=True)
        return

    tasks = [(s, a) for s in todo for a in ATS]
    hits: dict[str, tuple[str, int]] = {}  # slug -> (ats, jobs), keeping the best ats per slug
    done = 0
    with ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(probe_one, t): t for t in tasks}
        for fut in as_completed(futures):
            slug, ats, n = fut.result()
            done += 1
            if n and (slug not in hits or n > hits[slug][1]):
                hits[slug] = (ats, n)
                print(f"HIT {slug} | {ats} | {n} jobs", flush=True)
            if done % 1000 == 0:
                print(f"... {done}/{len(tasks)} requests done, {len(hits)} hits so far", flush=True)

    accepted, review = [], {}
    for slug, (ats, n) in hits.items():
        if len(slug) < MIN_SAFE_SLUG_LENGTH:
            review[slug] = {"ats": ats, "jobs": n, "reason": f"slug shorter than {MIN_SAFE_SLUG_LENGTH} chars - "
                                                             "higher risk of being an unrelated company's board"}
            continue
        name = yc_name(slug) or slug.replace("-", " ").title()
        accepted.append({"name": name, "ats": ats, "slug": slug})

    if accepted:
        careers.save_companies(existing + accepted)
        print(f"[discover] added {len(accepted)} newly-found companies to companies.json:", flush=True)
        for c in accepted:
            print(f"   + {c['name']} ({c['ats']}/{c['slug']})", flush=True)
    else:
        print("[discover] no new companies to add this run", flush=True)

    REVIEW_FILE.write_text(json.dumps(review, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if review:
        print(f"[discover] {len(review)} short-slug hits need manual review - see {REVIEW_FILE.name}", flush=True)


if __name__ == "__main__":
    main()
