"""Tests for discover.py's discover_pool() - the core "which hits get accepted" decision, shared by
both candidate pools (YC and NSE/BSE). No network access: probe_one and _recently_posted are
monkeypatched so the whole thing runs on fake, deterministic data.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discover


def fake_probe(hits: dict):
    """hits: {slug: job_count}. Every (slug, ats) combo not listed here probes as 0 (no board)."""
    def probe_one(task):
        slug, ats = task
        return slug, ats, hits.get(slug, 0)
    return probe_one


def test_accepts_a_hit_with_a_recent_posting(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", fake_probe({"acme-corp": 3}))
    monkeypatch.setattr(discover, "_recently_posted", lambda ats, slug: True)
    accepted, review = discover.discover_pool("TEST", ["acme-corp"], {}, [],
                                              lambda slug: "Acme Corp", workers=4, limit=None)
    assert [c["name"] for c in accepted] == ["Acme Corp"]
    assert review == {}


def test_skips_a_hit_with_no_recent_posting(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", fake_probe({"stale-corp": 1}))
    monkeypatch.setattr(discover, "_recently_posted", lambda ats, slug: False)
    accepted, review = discover.discover_pool("TEST", ["stale-corp"], {}, [],
                                              lambda slug: "Stale Corp", workers=4, limit=None)
    assert accepted == []
    assert review == {}


def test_short_slug_goes_to_review_instead_of_being_auto_added(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", fake_probe({"abc": 2}))  # 3 chars < MIN_SAFE_SLUG_LENGTH
    monkeypatch.setattr(discover, "_recently_posted", lambda ats, slug: True)
    accepted, review = discover.discover_pool("TEST", ["abc"], {}, [],
                                              lambda slug: "Abc Inc", workers=4, limit=None)
    assert accepted == []
    assert "abc" in review


def test_already_tracked_and_excluded_candidates_are_never_probed(monkeypatch):
    probed = []

    def probe_one(task):
        probed.append(task)
        return task[0], task[1], 5

    monkeypatch.setattr(discover, "probe_one", probe_one)
    monkeypatch.setattr(discover, "_recently_posted", lambda ats, slug: True)
    existing = [{"name": "Already Here", "ats": "greenhouse", "slug": "tracked-co"}]
    accepted, review = discover.discover_pool(
        "TEST", ["tracked-co", "excluded-co", "new-co"], {"excluded-co": "known collision"}, existing,
        lambda slug: slug, workers=4, limit=None)
    assert {t[0] for t in probed} == {"new-co"}
    assert [c["slug"] for c in accepted] == ["new-co"]


def test_name_resolver_falls_back_to_title_cased_slug(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", fake_probe({"some-startup": 1}))
    monkeypatch.setattr(discover, "_recently_posted", lambda ats, slug: True)
    accepted, _ = discover.discover_pool("TEST", ["some-startup"], {}, [],
                                         lambda slug: None, workers=4, limit=None)
    assert accepted[0]["name"] == "Some Startup"
