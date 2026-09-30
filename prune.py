"""Daily health-check on companies already in companies.json - the mirror image of discover.py.

discover.py only ever adds companies; nothing ever removes one that's stopped hiring, so it would sit
in the live app's list forever, costing an hourly fetch for nothing. This rechecks every company
already tracked, and any with no jobs for several consecutive daily runs gets moved out of
companies.json into pruned_companies.json. discover.py also rechecks that file each run (alongside
the YC candidate pool), so a company that starts hiring again later is automatically added straight
back - nothing is lost, just set aside while it's not useful.

A single empty day isn't enough to remove a company (network hiccups and one-off ATS failures happen
- see the "treated the same as empty" note below), which is why this tracks a streak rather than
acting on the first empty result.

Usage:
    python prune.py                    # check all companies, remove those past the threshold
    python prune.py --threshold 5      # require 5 consecutive empty days instead of the default 3
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import careers

STATE_FILE = Path(__file__).with_name("prune_state.json")
PRUNED_FILE = Path(__file__).with_name("pruned_companies.json")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def load_pruned() -> list[dict]:
    try:
        return json.loads(PRUNED_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def save_pruned(companies: list[dict]) -> None:
    # de-dup by (ats, slug), same convention as careers.save_companies
    companies = sorted({c["slug"].lower() + c["ats"]: c for c in companies}.values(), key=lambda c: c["name"].lower())
    PRUNED_FILE.write_text(json.dumps(companies, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def check_one(c: dict) -> tuple[str, int]:
    key = f"{c['ats']}/{c['slug']}"
    try:
        jobs = careers.fetch_board(c)
        return key, len(jobs)
    except Exception as e:
        print(f"[prune] {c['name']} ({c['ats']}) failed: {e!s:.100}", file=sys.stderr)
        return key, 0  # counted as "empty today" - the multi-day threshold absorbs one-off failures


def run(threshold: int = 3, workers: int = 12) -> dict:
    """The actual logic, separate from CLI parsing so it's directly callable/testable.
    Returns {"kept": [...], "removed": [...]} for the caller (tests, or main()) to inspect."""
    companies = careers.load_companies()
    state = load_state()
    print(f"[prune] checking {len(companies)} companies (removing after {threshold} consecutive empty days)",
         flush=True)

    with ThreadPoolExecutor(workers) as pool:
        results = dict(pool.map(check_one, companies))

    keep, remove = [], []
    for c in companies:
        key = f"{c['ats']}/{c['slug']}"
        n = results.get(key, 0)
        if n > 0:
            state.pop(key, None)  # has jobs again - reset any streak
            keep.append(c)
            continue
        streak = state.get(key, 0) + 1
        if streak >= threshold:
            print(f"[prune] removing {c['name']} ({c['ats']}/{c['slug']}) - empty for {streak} consecutive checks",
                 flush=True)
            remove.append(c)
            state.pop(key, None)
        else:
            state[key] = streak
            keep.append(c)  # not yet at the threshold - keep it for now

    save_state(state)
    # Written unconditionally, even when empty - a CI step downstream does `git add pruned_companies.json`,
    # which fails outright if the file has never existed yet (as opposed to existing with no content).
    save_pruned(load_pruned() + remove)
    if remove:
        careers.save_companies(keep)
        print(f"[prune] removed {len(remove)} companies with no current jobs; {len(keep)} remain tracked", flush=True)
    else:
        print("[prune] nothing to remove this run", flush=True)
    return {"kept": keep, "removed": remove}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=int, default=3, help="consecutive empty days before removing a company")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    run(args.threshold, args.workers)


if __name__ == "__main__":
    main()
