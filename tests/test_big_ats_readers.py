"""Tests for the India-only readers of the big ATS platforms in careers.py: Workday, Amazon and Oracle
HCM. They are the most intricate readers (facet discovery, pagination, location rewriting), so they get
their own file. Like test_scrapers.py, `requests` is faked - no network.

Run:  pytest tests/test_big_ats_readers.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import careers
from test_scrapers import FakeResp, serve, serve_one  # same fake HTTP layer as the other scraper tests


@pytest.fixture(autouse=True)
def _clean_careers_state(monkeypatch):
    careers._board_cache.clear()
    careers._failed.clear()
    monkeypatch.setattr(careers.time, "sleep", lambda s: None)


# --- Workday -------------------------------------------------------------------------------------

def _facet(*values, param="locationCountry"):
    return {"facetParameter": param, "values": list(values)}


def test_find_india_facet_prefers_exact_india_value():
    facets = [_facet({"descriptor": "United States", "id": "us"}, {"descriptor": "India", "id": "in1"})]
    assert careers._find_india_facet(facets) == ("locationCountry", ["in1"])


def test_find_india_facet_exact_india_beats_city_labels_even_on_another_param():
    facets = [_facet({"descriptor": "Bengaluru", "id": "city", "count": 99}, param="locations"),
              _facet({"descriptor": "India", "id": "country", "count": 1}, param="locationCountry")]
    assert careers._find_india_facet(facets) == ("locationCountry", ["country"])


def test_find_india_facet_finds_value_inside_nested_group():
    facets = [{"facetParameter": "locationHierarchy1", "values": [
        {"facetParameter": "locations", "values": [{"descriptor": "India", "id": "deep"}]}]}]
    assert careers._find_india_facet(facets) == ("locations", ["deep"])


def test_find_india_facet_falls_back_to_site_specific_labels_on_the_biggest_param():
    facets = [
        _facet({"descriptor": "IND-Bangalore Campus", "id": "a", "count": 5},
               {"descriptor": "Bengaluru", "id": "b", "count": 7}, param="locations"),
        _facet({"descriptor": "IND19-01-Pune-Tower", "id": "c", "count": 1}, param="other"),
    ]
    assert careers._find_india_facet(facets) == ("locations", ["a", "b"])  # param with most jobs, all its ids


def test_find_india_facet_none_when_no_india():
    assert careers._find_india_facet([_facet({"descriptor": "Germany", "id": "de"})]) is None
    assert careers._find_india_facet(None) is None


@pytest.mark.parametrize("text,expected", [
    ("IND-Bangalore", True), ("IND19-01-Bengaluru-Tower", True), ("IN-KA-Bangalore", True),
    ("Indianapolis, IN", False),  # a trailing ", IN" is Indiana and must not match
    ("Indiana", False), ("Austin, TX", False)])
def test_india_location_patterns(text, expected):
    assert bool(careers._INDIA_RE.search(text) or careers._IND_CODE_RE.search(text)) is expected


def _workday_handler(facets, postings_pages, calls=None):
    """First POST (no applied facets) returns the facet list; later ones return a page of postings."""
    def handler(url, **kw):
        body = kw["json"]
        if calls is not None:
            calls.append(json.loads(json.dumps(body)))
        if not body["appliedFacets"]:
            return FakeResp({"facets": facets})
        page = body["offset"] // 20
        return FakeResp({"jobPostings": postings_pages[page] if page < len(postings_pages) else []})
    return handler


def test_workday_applies_india_facet_and_builds_urls(monkeypatch):
    calls = []
    posts = [[{"title": "SDE", "locationsText": "Bengaluru", "externalPath": "/job/Bengaluru/SDE_R1",
               "postedOn": "Posted 3 Days Ago"},
              {"title": "PM", "locationsText": "3 Locations", "externalPath": "/job/x/PM_R2"},
              {"title": "QA", "locationsText": "Remote, India", "externalPath": "/job/x/QA_R3"}]]
    serve(monkeypatch, _workday_handler([_facet({"descriptor": "India", "id": "in1"})], posts, calls))
    a, b, c = careers.workday("acme/wd5/External", "Acme")
    assert calls[1]["appliedFacets"] == {"locationCountry": ["in1"]}
    assert a["location"] == "Bengaluru, India" and a["posted"] == "Posted 3 Days Ago"
    assert a["url"] == "https://acme.wd5.myworkdayjobs.com/External/job/Bengaluru/SDE_R1"
    assert b["location"] == "India (multiple locations)"  # "3 Locations" must not become "3 Locations, India"
    assert c["location"] == "Remote, India" and c["remote"] is True


def test_workday_no_india_facet_returns_empty_after_one_call(monkeypatch):
    calls = []
    serve(monkeypatch, _workday_handler([_facet({"descriptor": "Germany", "id": "de"})], [], calls))
    assert careers.workday("acme/wd5/External", "Acme") == []
    assert len(calls) == 1


def test_workday_paginates_by_20_and_respects_max_jobs(monkeypatch):
    full = [{"title": f"J{i}", "locationsText": "Pune", "externalPath": f"/j{i}"} for i in range(20)]
    calls = []
    serve(monkeypatch, _workday_handler([_facet({"descriptor": "India", "id": "i"})], [full, full, full], calls))
    jobs = careers.workday("acme/wd5/External", "Acme", max_jobs=40)
    assert len(jobs) == 40
    assert [c["offset"] for c in calls[1:]] == [0, 20]


def test_workday_short_page_stops(monkeypatch):
    calls = []
    serve(monkeypatch, _workday_handler([_facet({"descriptor": "India", "id": "i"})],
                                        [[{"title": "J", "locationsText": "Pune"}]], calls))
    assert len(careers.workday("acme/wd5/External", "Acme")) == 1
    assert len(calls) == 2  # facet probe + one page; no second page fetched


# --- Amazon --------------------------------------------------------------------------------------

def test_amazon(monkeypatch):
    seen = {}

    def handler(url, **kw):
        seen.update(kw["params"])
        return FakeResp({"jobs": [
            {"title": "SDE II", "normalized_location": "Hyderabad, TG, IND", "job_path": "/en/jobs/123/sde",
             "posted_date": "October 5, 2026", "basic_qualifications": "3+ years<br/>Java",
             "job_schedule_type": "full-time", "business_category": "Software"},
            {"title": "No date", "location": "Pune, MH, IND", "posted_date": "garbage"}]})
    serve(monkeypatch, handler)
    a, b = careers.amazon("", "Amazon")
    assert seen["country"] == "IND" and seen["sort"] == "recent"
    assert a["location"] == "Hyderabad, TG, India" and a["posted"] == "2026-10-05"
    assert a["url"] == "https://www.amazon.jobs/en/jobs/123/sde" and a["job_type"] == "full-time"
    assert "3+ years" in a["description"] and a["department"] == "Software"
    assert b["posted"] == "" and b["location"] == "Pune, MH, India"  # bad date tolerated; falls back to "location"


def test_amazon_paginates_by_100(monkeypatch):
    offsets = []

    def handler(url, **kw):
        offsets.append(kw["params"]["offset"])
        n = 100 if kw["params"]["offset"] == 0 else 3
        return FakeResp({"jobs": [{"title": "J", "location": "Pune", "job_path": "/j"}] * n})
    serve(monkeypatch, handler)
    assert len(careers.amazon("", "Amazon", max_jobs=300)) == 103
    assert offsets == [0, 100]


# --- Oracle HCM ----------------------------------------------------------------------------------

def _oracle(finders, facet=None, pages=None):
    """Fake Oracle endpoint: the limit=1 probe returns the location facet; later pages return `pages`."""
    pages = pages or [[]]

    def handler(url, **kw):
        finder = kw["params"]["finder"]
        finders.append(finder)
        if finder.endswith("limit=1"):
            return FakeResp({"items": [{"locationsFacet": facet or []}]})
        page = int(finder.split("offset=")[1].split(",")[0]) // 25
        return FakeResp({"items": [{"requisitionList": pages[page] if page < len(pages) else []}]})
    return handler


REQ = {"Id": "42", "Title": "Analyst", "PrimaryLocation": "Mumbai, India", "PrimaryLocationCountry": "IN",
       "PostedDate": "2026-10-01", "ShortDescriptionStr": "Short", "ExternalQualificationsStr": "5 years exp",
       "JobSchedule": "Full time", "WorkplaceType": "Remote", "JobFamily": "Finance"}


def test_oraclehcm_uses_india_facet_id_when_listed(monkeypatch):
    finders = []
    serve(monkeypatch, _oracle(finders, facet=[{"Name": "United States", "Id": "1"}, {"Name": "India", "Id": "300"}],
                               pages=[[REQ]]))
    [j] = careers.oraclehcm("jpmc.fa.oraclecloud.com/CX_1001", "JPMC")
    assert "selectedLocationsFacet=300" in finders[1] and "siteNumber=CX_1001" in finders[1]
    assert (j["title"], j["location"], j["department"], j["job_type"]) == (
        "Analyst", "Mumbai, India", "Finance", "Full time")
    assert j["remote"] is True and "5 years" in j["description"]
    assert j["url"] == "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/42"


def test_oraclehcm_without_facet_searches_by_name_and_drops_non_india(monkeypatch):
    finders = []
    foreign = {**REQ, "Id": "43", "PrimaryLocation": "Paris", "PrimaryLocationCountry": "FR"}
    serve(monkeypatch, _oracle(finders, facet=[{"Name": "United States", "Id": "1"}], pages=[[REQ, foreign]]))
    jobs = careers.oraclehcm("h.fa.oraclecloud.com/CX_1", "H")
    assert "location=India" in finders[1]
    assert [j["title"] for j in jobs] == ["Analyst"]  # the French posting was filtered out


def test_oraclehcm_paginates_by_25(monkeypatch):
    finders = []
    page = [dict(REQ, Id=str(i)) for i in range(25)]
    serve(monkeypatch, _oracle(finders, facet=[{"Name": "India", "Id": "9"}], pages=[page, [REQ]]))
    assert len(careers.oraclehcm("h.fa.oraclecloud.com/CX_1", "H", max_jobs=200)) == 26
    assert any("offset=25" in f for f in finders)


def test_oraclehcm_empty_reply_returns_empty(monkeypatch):
    serve_one(monkeypatch, FakeResp({"items": []}))
    assert careers.oraclehcm("h.fa.oraclecloud.com/CX_1", "H") == []
