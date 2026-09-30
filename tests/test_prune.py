"""Tests for prune.py (removing companies with no current jobs after several consecutive checks) and
discover.py's re-check of previously-pruned companies. No network calls: careers.fetch_board is
monkeypatched to a fake per test.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import careers
import discover
import prune


def company(name, ats="greenhouse", slug=None):
    return {"name": name, "ats": ats, "slug": slug or name.lower()}


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Isolates prune.py/careers.py's files to a temp directory and returns a helper namespace."""
    monkeypatch.setattr(prune, "STATE_FILE", tmp_path / "prune_state.json")
    monkeypatch.setattr(prune, "PRUNED_FILE", tmp_path / "pruned_companies.json")
    monkeypatch.setattr(discover, "PRUNED_FILE", tmp_path / "pruned_companies.json")
    monkeypatch.setattr(careers, "COMPANIES_FILE", tmp_path / "companies.json")
    return tmp_path


def fake_fetch(job_counts: dict):
    """A drop-in for careers.fetch_board keyed by company name, for tests to control per-company results."""
    def fetch(c):
        n = job_counts.get(c["name"], 0)
        if n == -1:  # sentinel: simulate a hard failure (e.g. the board is unreachable)
            raise RuntimeError("simulated fetch failure")
        return [{"title": f"job {i}"} for i in range(n)]
    return fetch


def test_company_with_jobs_is_kept(sandbox, monkeypatch):
    careers.save_companies([company("Acme")])
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"Acme": 3}))
    result = prune.run(threshold=3)
    assert [c["name"] for c in result["kept"]] == ["Acme"]
    assert result["removed"] == []
    assert len(careers.load_companies()) == 1


def test_company_not_removed_before_threshold(sandbox, monkeypatch):
    careers.save_companies([company("DriedUp")])
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"DriedUp": 0}))
    prune.run(threshold=3)  # day 1: empty
    prune.run(threshold=3)  # day 2: empty
    still_tracked = careers.load_companies()
    assert len(still_tracked) == 1, "should not be removed before 3 consecutive empty checks"


def test_company_removed_after_threshold_consecutive_empty_checks(sandbox, monkeypatch):
    careers.save_companies([company("DriedUp")])
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"DriedUp": 0}))
    for _ in range(3):
        prune.run(threshold=3)
    assert careers.load_companies() == []
    pruned = json.loads(prune.PRUNED_FILE.read_text(encoding="utf-8"))
    assert [c["name"] for c in pruned] == ["DriedUp"]


def test_streak_resets_when_jobs_reappear(sandbox, monkeypatch):
    careers.save_companies([company("Seasonal")])
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"Seasonal": 0}))
    prune.run(threshold=3)  # empty day 1
    prune.run(threshold=3)  # empty day 2
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"Seasonal": 5}))
    prune.run(threshold=3)  # has jobs again - streak should reset
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"Seasonal": 0}))
    prune.run(threshold=3)  # empty day 1 again (not day 3!) - should NOT be removed yet
    prune.run(threshold=3)  # empty day 2
    assert len(careers.load_companies()) == 1, "streak should have reset when jobs reappeared, not carried over"


def test_fetch_failure_counts_as_empty_but_does_not_crash(sandbox, monkeypatch):
    careers.save_companies([company("Flaky")])
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"Flaky": -1}))
    for _ in range(3):
        result = prune.run(threshold=3)
    assert result["removed"] and result["removed"][0]["name"] == "Flaky"


def test_multiple_companies_handled_independently(sandbox, monkeypatch):
    careers.save_companies([company("Alive"), company("Dead")])
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"Alive": 10, "Dead": 0}))
    for _ in range(3):
        prune.run(threshold=3)
    remaining = {c["name"] for c in careers.load_companies()}
    assert remaining == {"Alive"}


def test_discover_revives_a_pruned_company_that_is_hiring_again(sandbox, monkeypatch, tmp_path):
    discover.PRUNED_FILE.write_text(json.dumps([company("Revived")]), encoding="utf-8")
    careers.save_companies([])
    # point discover at an empty YC candidate list so this test only exercises the pruned-recheck path
    monkeypatch.setattr(discover, "CANDIDATES_FILE", tmp_path / "yc_candidates.json")
    discover.CANDIDATES_FILE.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(discover, "EXCLUDED_FILE", tmp_path / "yc_excluded.json")
    monkeypatch.setattr(discover, "REVIEW_FILE", tmp_path / "yc_needs_review.json")
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"Revived": 4}))

    old_argv = sys.argv
    sys.argv = ["discover.py"]
    try:
        discover.main()
    finally:
        sys.argv = old_argv

    tracked = careers.load_companies()
    assert [c["name"] for c in tracked] == ["Revived"]
    assert json.loads(discover.PRUNED_FILE.read_text(encoding="utf-8")) == []


def test_discover_leaves_pruned_company_alone_if_still_empty(sandbox, monkeypatch, tmp_path):
    discover.PRUNED_FILE.write_text(json.dumps([company("StillDead")]), encoding="utf-8")
    careers.save_companies([])
    monkeypatch.setattr(discover, "CANDIDATES_FILE", tmp_path / "yc_candidates.json")
    discover.CANDIDATES_FILE.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(discover, "EXCLUDED_FILE", tmp_path / "yc_excluded.json")
    monkeypatch.setattr(discover, "REVIEW_FILE", tmp_path / "yc_needs_review.json")
    monkeypatch.setattr(careers, "fetch_board", fake_fetch({"StillDead": 0}))

    old_argv = sys.argv
    sys.argv = ["discover.py"]
    try:
        discover.main()
    finally:
        sys.argv = old_argv

    assert careers.load_companies() == []
    assert [c["name"] for c in json.loads(discover.PRUNED_FILE.read_text(encoding="utf-8"))] == ["StillDead"]
