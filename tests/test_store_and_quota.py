"""Tests for store.py (saved searches / bookmarks) and quota.py (API quota tracking).

Uses a throwaway sqlite file and quota-state file per test run (monkeypatched paths) so these tests
never touch the real job_scraper.db / quota_state.json a running app is using.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture()
def temp_store(tmp_path, monkeypatch):
    import store
    monkeypatch.setattr(store, "DB_FILE", tmp_path / "test.db")
    store.init()
    return store


@pytest.fixture()
def temp_quota(tmp_path, monkeypatch):
    import quota
    monkeypatch.setattr(quota, "QUOTA_FILE", tmp_path / "quota.json")
    quota._search_times.clear()
    return quota


def test_saved_search_roundtrip(temp_store):
    vid = temp_store.new_visitor_id()
    sid = temp_store.add_saved_search(vid, {"role": "python developer"}, "https://example.com/hook")
    found = temp_store.list_saved_searches(vid)
    assert len(found) == 1
    assert found[0]["id"] == sid
    assert found[0]["query"]["role"] == "python developer"


def test_saved_search_isolated_per_visitor(temp_store):
    v1, v2 = temp_store.new_visitor_id(), temp_store.new_visitor_id()
    temp_store.add_saved_search(v1, {"role": "a"})
    assert temp_store.list_saved_searches(v2) == []
    assert len(temp_store.list_saved_searches(v1)) == 1


def test_remove_saved_search(temp_store):
    vid = temp_store.new_visitor_id()
    sid = temp_store.add_saved_search(vid, {"role": "a"})
    temp_store.remove_saved_search(sid)
    assert temp_store.list_saved_searches(vid) == []


def test_filter_new_only_reports_unseen_urls_once(temp_store):
    vid = temp_store.new_visitor_id()
    sid = temp_store.add_saved_search(vid, {"role": "a"})
    first = temp_store.filter_new(sid, ["http://a", "http://b"])
    assert set(first) == {"http://a", "http://b"}
    second = temp_store.filter_new(sid, ["http://a", "http://b"])
    assert second == []
    third = temp_store.filter_new(sid, ["http://a", "http://c"])
    assert third == ["http://c"]  # only the genuinely new one


def test_job_actions_bookmark_and_unbookmark(temp_store):
    vid = temp_store.new_visitor_id()
    temp_store.set_action(vid, "http://job1", "bookmarked")
    assert temp_store.get_actions(vid) == {"http://job1": {"bookmarked"}}
    temp_store.set_action(vid, "http://job1", "bookmarked", on=False)
    assert temp_store.get_actions(vid) == {}


def test_job_actions_multiple_actions_same_job(temp_store):
    vid = temp_store.new_visitor_id()
    temp_store.set_action(vid, "http://job1", "bookmarked")
    temp_store.set_action(vid, "http://job1", "applied")
    assert temp_store.get_actions(vid)["http://job1"] == {"bookmarked", "applied"}


def test_quota_allows_until_limit(temp_quota):
    temp_quota.DAILY_LIMITS["testsrc"] = 2
    assert temp_quota.allow("testsrc") is True
    assert temp_quota.allow("testsrc") is True
    assert temp_quota.allow("testsrc") is False  # third call exceeds the limit of 2
    del temp_quota.DAILY_LIMITS["testsrc"]


def test_quota_unmetered_source_always_allowed(temp_quota):
    assert temp_quota.allow("some_source_with_no_limit_configured") is True


def test_quota_usage_reports_remaining(temp_quota):
    temp_quota.DAILY_LIMITS["testsrc2"] = 5
    temp_quota.allow("testsrc2")
    temp_quota.allow("testsrc2")
    assert temp_quota.usage()["testsrc2"] == {"used": 2, "limit": 5, "remaining": 3}
    del temp_quota.DAILY_LIMITS["testsrc2"]


def test_rate_limit_trips_after_max_searches(temp_quota):
    temp_quota.MAX_SEARCHES_PER_MINUTE = 3
    assert temp_quota.metered_sources_allowed() is True
    assert temp_quota.metered_sources_allowed() is True
    assert temp_quota.metered_sources_allowed() is True
    assert temp_quota.metered_sources_allowed() is False  # 4th call within the same minute
