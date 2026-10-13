"""Tests for discover.py's discover_pool() - the core "which hits get accepted" decision, shared by
both candidate pools (YC and NSE/BSE). No network access: probe_one and _verify_hit are monkeypatched
so the whole thing runs on fake, deterministic data.
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
    monkeypatch.setattr(discover, "_verify_hit", lambda ats, slug: (True, True))
    accepted, review = discover.discover_pool("TEST", ["acme-corp"], {}, [],
                                              lambda slug: "Acme Corp", workers=4, limit=None)
    assert [c["name"] for c in accepted] == ["Acme Corp"]
    assert review == {}


def test_skips_a_hit_with_no_recent_posting(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", fake_probe({"stale-corp": 1}))
    monkeypatch.setattr(discover, "_verify_hit", lambda ats, slug: (False, True))
    accepted, review = discover.discover_pool("TEST", ["stale-corp"], {}, [],
                                              lambda slug: "Stale Corp", workers=4, limit=None)
    assert accepted == []
    assert review == {}


def test_short_slug_goes_to_review_instead_of_being_auto_added(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", fake_probe({"abc": 2}))  # 3 chars < MIN_SAFE_SLUG_LENGTH
    monkeypatch.setattr(discover, "_verify_hit", lambda ats, slug: (True, True))
    accepted, review = discover.discover_pool("TEST", ["abc"], {}, [],
                                              lambda slug: "Abc Inc", workers=4, limit=None)
    assert accepted == []
    assert "abc" in review


def test_unverified_owner_goes_to_review_instead_of_being_auto_added(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", fake_probe({"acme-corp": 3}))
    monkeypatch.setattr(discover, "_verify_hit", lambda ats, slug: (True, True))
    accepted, review = discover.discover_pool("TEST", ["acme-corp"], {}, [], lambda slug: "Acme Corp",
                                              workers=4, limit=None, owner_check=lambda s, a: "no tie to acme.com")
    assert accepted == []
    assert review["acme-corp"]["reason"] == "no tie to acme.com"


def test_verified_owner_is_auto_added(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", fake_probe({"acme-corp": 3}))
    monkeypatch.setattr(discover, "_verify_hit", lambda ats, slug: (True, True))
    accepted, review = discover.discover_pool("TEST", ["acme-corp"], {}, [], lambda slug: "Acme Corp",
                                              workers=4, limit=None, owner_check=lambda s, a: None)
    assert [c["slug"] for c in accepted] == ["acme-corp"]
    assert review == {}


def test_domain_label():
    assert discover._domain_label("https://www.acme.com/about") == "acme"
    assert discover._domain_label("http://acme.co.uk") == "acme"
    assert discover._domain_label("acme.io") == "acme"
    assert discover._domain_label("localhost") is None


def _owner(monkeypatch, site, jobs):
    monkeypatch.setattr(discover, "yc_website", lambda slug: site)
    monkeypatch.setattr(discover.careers, "fetch_board", lambda c: jobs)
    return discover.verify_yc_owner("acme-corp", "greenhouse")


def test_verify_owner_accepts_job_on_company_domain(monkeypatch):
    jobs = [{"url": "https://careers.acme.com/apply/1", "title": "Engineer", "description": ""}]
    assert _owner(monkeypatch, "https://acme.com", jobs) is None


def test_verify_owner_accepts_domain_mentioned_in_description(monkeypatch):
    jobs = [{"url": "https://boards.greenhouse.io/acme-corp/1", "title": "Engineer",
             "description": "Join us - see acme.com/careers"}]
    assert _owner(monkeypatch, "https://acme.com", jobs) is None


def test_verify_owner_rejects_colliding_company(monkeypatch):
    jobs = [{"url": "https://boards.greenhouse.io/acme-corp/1", "title": "Vet Nurse",
             "description": "Ark Veterinary Hospital, see arkvet.com"}]
    assert _owner(monkeypatch, "https://acme.com", jobs) is not None


def test_verify_owner_rejects_when_website_unknown(monkeypatch):
    assert _owner(monkeypatch, None, [{"url": "x", "title": "t", "description": ""}]) is not None


def test_already_tracked_and_excluded_candidates_are_never_probed(monkeypatch):
    probed = []

    def probe_one(task):
        probed.append(task)
        return task[0], task[1], 5

    monkeypatch.setattr(discover, "probe_one", probe_one)
    monkeypatch.setattr(discover, "_verify_hit", lambda ats, slug: (True, True))
    existing = [{"name": "Already Here", "ats": "greenhouse", "slug": "tracked-co"}]
    accepted, review = discover.discover_pool(
        "TEST", ["tracked-co", "excluded-co", "new-co"], {"excluded-co": "known collision"}, existing,
        lambda slug: slug, workers=4, limit=None)
    assert {t[0] for t in probed} == {"new-co"}
    assert [c["slug"] for c in accepted] == ["new-co"]


def test_name_resolver_falls_back_to_title_cased_slug(monkeypatch):
    monkeypatch.setattr(discover, "probe_one", fake_probe({"some-startup": 1}))
    monkeypatch.setattr(discover, "_verify_hit", lambda ats, slug: (True, True))
    accepted, _ = discover.discover_pool("TEST", ["some-startup"], {}, [],
                                         lambda slug: None, workers=4, limit=None)
    assert accepted[0]["name"] == "Some Startup"


# --- require_india_location=True (the NSE/BSE pool) ----------------------------------------------

def test_india_pool_never_auto_accepts_even_a_good_hit(monkeypatch):
    """The whole point of this pool: ticker-slug matches are too unreliable to auto-add, ever - even a
    hit that looks perfectly legitimate (recent postings, has an India office) only ever goes to review."""
    monkeypatch.setattr(discover, "probe_one", fake_probe({"realco": 5}))
    monkeypatch.setattr(discover, "_verify_hit", lambda ats, slug: (True, True))
    accepted, review = discover.discover_pool("NSE", ["realco"], {}, [], lambda slug: "Real Co",
                                              workers=4, limit=None, require_india_location=True)
    assert accepted == []
    assert "realco" in review


def test_india_pool_discards_a_hit_with_no_india_posting(monkeypatch):
    """Real case found in production: NSE ticker "indigo" matched a US company's Ashby board (no India
    postings at all) - completely unrelated to IndiGo Airlines. A hit with zero India-based postings is
    discarded outright, not even sent to review."""
    monkeypatch.setattr(discover, "probe_one", fake_probe({"indigo": 26}))
    monkeypatch.setattr(discover, "_verify_hit", lambda ats, slug: (True, False))
    accepted, review = discover.discover_pool("NSE", ["indigo"], {}, [], lambda slug: "IndiGo",
                                              workers=4, limit=None, require_india_location=True)
    assert accepted == []
    assert review == {}


def _yc_page(title, *names):
    data = "".join(f"&quot;name&quot;:&quot;{n}&quot;," for n in names)
    return f"<html><head><title>{title}</title></head><body>{data}</body></html>"


def test_yc_name_from_simple_title():
    assert discover.parse_yc_name(_yc_page("Stripe: Economic infrastructure | Y Combinator", "Stripe")) == "Stripe"


def test_yc_name_keeps_colon_and_pipe_in_the_real_name():
    page = _yc_page("Foo: Bar Labs: Tools for devs | Y Combinator", "Foo: Bar Labs")
    assert discover.parse_yc_name(page) == "Foo: Bar Labs"


def test_yc_name_unescapes_html_entities():
    page = _yc_page("AT&amp;T Labs: Phones | Y Combinator", "AT&amp;T Labs")
    assert discover.parse_yc_name(page) == "AT&T Labs"


def test_yc_name_falls_back_to_title_split_without_json_data():
    assert discover.parse_yc_name(_yc_page("Scale AI: Data infrastructure | Y Combinator")) == "Scale AI"


def test_yc_name_ignores_unrelated_json_name():
    # the embedded name doesn't match the title (e.g. a founder), so don't trust it
    page = _yc_page("Acme: Widgets | Y Combinator", "Jane Founder")
    assert discover.parse_yc_name(page) == "Acme"


def test_yc_name_none_for_missing_or_generic_pages():
    assert discover.parse_yc_name("<html>no title</html>") is None
    assert discover.parse_yc_name(_yc_page("Y Combinator | File Not Found")) is None


def test_yc_name_404_is_not_retried(monkeypatch):
    calls = []

    class R:
        status_code, text = 404, "<title>Y Combinator | File Not Found</title>"
    monkeypatch.setattr(discover.requests, "get", lambda *a, **k: calls.append(1) or R())
    assert discover.yc_name("gone") is None
    assert len(calls) == 1
