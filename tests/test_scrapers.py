"""Tests for the per-source scrapers (job_scraper.py) and ATS readers (careers.py).

Each test feeds a scraper a small canned response shaped like the real API/page, with `requests`
monkeypatched - no network. The point is to catch the two ways these break: our own parsing
regressing, and (when a fixture is refreshed from a live response) a site changing its format. A
scraper that "breaks silently" returns [] - so most tests assert a field actually came through, and
each source also has a test that malformed/empty replies return [] rather than raising.

Run:  pytest tests/test_scrapers.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import requests

import careers
import job_scraper as js


class FakeResp:
    def __init__(self, payload=None, status=200, text=None, url="", headers=None):
        self._payload = payload
        self.status_code = status
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")
        self.url = url
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def serve(monkeypatch, handler):
    """Routes every requests.get/post through handler(url, **kwargs) -> FakeResp."""
    monkeypatch.setattr(requests, "get", lambda url, **kw: handler(url, **kw))
    monkeypatch.setattr(requests, "post", lambda url, **kw: handler(url, **kw))


def serve_one(monkeypatch, resp):
    serve(monkeypatch, lambda url, **kw: resp)


@pytest.fixture(autouse=True)
def _no_quota_or_cache(monkeypatch):
    monkeypatch.setattr(js.quota, "allow", lambda *a, **k: True)
    js._jobvetta_cache.clear()
    careers._board_cache.clear()
    careers._failed.clear()
    monkeypatch.setattr(careers.time, "sleep", lambda s: None)
    monkeypatch.setattr(js.time, "sleep", lambda s: None)


# --- keyless JSON APIs ---------------------------------------------------------------------------

def test_remotive(monkeypatch):
    serve_one(monkeypatch, FakeResp({"jobs": [{
        "title": "Python Developer", "company_name": "Acme", "candidate_required_location": "Worldwide",
        "url": "https://remotive.com/j/1", "description": "<p>Build <b>APIs</b></p>",
        "publication_date": "2026-10-01T10:00:00", "tags": ["python"], "job_type": "full_time",
        "salary": "$100k - $120k"}]}))
    [j] = js.remotive("python")
    assert (j.title, j.company, j.source, j.remote) == ("Python Developer", "Acme", "remotive", True)
    assert j.url == "https://remotive.com/j/1"
    assert "Worldwide" in j.location and "<" not in j.description
    assert j.tags == ["python"]


def test_remotive_empty(monkeypatch):
    serve_one(monkeypatch, FakeResp({"jobs": []}))
    assert js.remotive("python") == []


def test_arbeitnow_stops_on_error_status(monkeypatch):
    pages = []

    def handler(url, **kw):
        pages.append(kw["params"]["page"])
        if kw["params"]["page"] == 1:
            return FakeResp({"data": [{"title": "Dev", "company_name": "Beta", "location": "Berlin",
                                       "remote": True, "url": "https://a/1", "description": "<i>x</i>",
                                       "created_at": 1760000000, "tags": [], "job_types": ["full time"]}]})
        return FakeResp(status=503)
    serve(monkeypatch, handler)
    jobs = js.arbeitnow("dev")
    assert [j.title for j in jobs] == ["Dev"]
    assert jobs[0].location == "Berlin (Remote)" and jobs[0].remote is True
    assert jobs[0].posted.startswith("2025-10")  # epoch converted to an ISO date
    assert pages == [1, 2]  # stopped at the first non-200 page


def test_remoteok_skips_legal_notice_row(monkeypatch):
    serve_one(monkeypatch, FakeResp([
        {"legal": "terms of use"},  # RemoteOK's first element is not a job
        {"position": "Rust Engineer", "company": "Gamma", "location": "", "url": "https://r/1",
         "description": "<p>hi</p>", "date": "2026-10-02T00:00:00+00:00", "tags": ["rust"],
         "salary_min": 90000, "salary_max": 120000}]))
    [j] = js.remoteok("rust")
    assert (j.title, j.company, j.source) == ("Rust Engineer", "Gamma", "remoteok")
    assert j.location == "Worldwide (Remote)"


def test_jobicy(monkeypatch):
    serve_one(monkeypatch, FakeResp({"jobs": [{
        "jobTitle": "Data Analyst", "companyName": "Delta", "jobGeo": "USA", "url": "https://j/1",
        "jobExcerpt": "Great job", "jobDescription": "<p>Analyze</p>", "pubDate": "2026-10-03 09:00:00",
        "jobLevel": "Senior", "jobType": ["Full-Time"], "salaryMin": 80000, "salaryMax": 100000,
        "salaryCurrency": "USD", "salaryPeriod": "yearly"}]}))
    [j] = js.jobicy("analyst")
    assert (j.title, j.company, j.level) == ("Data Analyst", "Delta", "senior")
    assert j.location == "USA (Remote)"


def test_himalayas_maps_seniority_and_remote(monkeypatch):
    serve_one(monkeypatch, FakeResp({"jobs": [{
        "title": "Backend Engineer", "companyName": "Epsilon", "locationRestrictions": ["India", "UK"],
        "applicationLink": "https://h/apply", "excerpt": "e", "description": "<p>d</p>",
        "pubDate": 1760000000, "seniority": ["Mid-level"], "categories": ["Engineering"],
        "employmentType": "Full Time", "minSalary": 1000000, "maxSalary": 2000000, "currency": "INR",
        "salaryPeriod": "year"}]}))
    [j] = js.himalayas("backend", ["bangalore"])
    assert j.level == "mid" and j.remote is True
    assert j.location == "India, UK (Remote)"
    assert j.url == "https://h/apply"


def test_himalayas_india_location_sets_country_param(monkeypatch):
    seen = {}

    def handler(url, **kw):
        seen.update(kw["params"])
        return FakeResp({"jobs": []})
    serve(monkeypatch, handler)
    js.himalayas("backend", ["bangalore"])
    assert seen.get("country") == "India"


# --- India sources -------------------------------------------------------------------------------

def test_instahyre(monkeypatch):
    serve_one(monkeypatch, FakeResp({"objects": [{
        "title": "Backend Developer", "employer": {"company_name": "Zeta", "instahyre_note": "note"},
        "locations": "Bangalore,Pune", "public_url": "https://instahyre.com/job/1", "keywords": ["python"]}]}))
    [j] = js.instahyre("backend")
    assert (j.title, j.company, j.source) == ("Backend Developer", "Zeta", "instahyre")
    assert j.location == "Bangalore, Pune, India"
    assert j.url == "https://instahyre.com/job/1"


def test_instahyre_missing_employer_does_not_crash(monkeypatch):
    serve_one(monkeypatch, FakeResp({"objects": [{"title": "X", "employer": None, "locations": None}]}))
    [j] = js.instahyre("x")
    assert j.company == "" and j.location.endswith("India")


def _next_data_page(jobs):
    blob = json.dumps({"props": {"pageProps": {"data": {"jobs": jobs}}}})
    return f'<html><script id="__NEXT_DATA__" type="application/json">{blob}</script></html>'


def test_cutshort_reads_next_data(monkeypatch):
    job = {"_id": "65a1b2c3d4e5f60718293a4b", "headline": "Python Dev", "companyDetails": {"name": "Eta"},
           "locations": ["Bangalore"], "publicUrl": "https://cutshort.io/job/1", "sanitizedComment": "<p>hi</p>",
           "expRange": {"min": 2, "max": 5}, "salaryRange": {"min": 1000000, "max": 2000000, "currency": "INR"},
           "allSkills": ["python"], "roleTypes": ["Full-time"], "remoteType": "remote_okay"}
    serve_one(monkeypatch, FakeResp(text=_next_data_page([job])))
    [j] = js.cutshort("python developer")
    assert (j.title, j.company, j.exp_min, j.exp_max) == ("Python Dev", "Eta", 2, 5)
    assert j.remote is True and j.location == "Bangalore, India"
    assert j.posted  # derived from the Mongo ObjectId timestamp


def test_cutshort_hidden_salary_is_dropped(monkeypatch):
    job = {"headline": "Dev", "salaryRange": {"min": 1, "max": 2, "currency": "INR", "hideSalary": True}}
    serve_one(monkeypatch, FakeResp(text=_next_data_page([job])))
    [j] = js.cutshort("dev")
    assert j.salary_min is None and j.salary_max is None


def test_cutshort_layout_change_returns_empty_not_error(monkeypatch):
    serve_one(monkeypatch, FakeResp(text="<html><body>redesigned, no __NEXT_DATA__</body></html>"))
    assert js.cutshort("python") == []


INTERNSHALA_CARD = """
<div class="individual_internship" internshipid="1" employment_type="job">
  <a class="job-title-href" href="/job/detail/python-dev-acme">Python Developer</a>
  <div class="company-name">Acme Pvt Ltd</div>
  <div class="locations"><a>Bangalore</a><a>Pune</a></div>
  <div class="row-1-item"><i class="ic-16-briefcase"></i><span>0-1 years</span></div>
  <div class="row-1-item"><i class="ic-16-money"></i><span>3 - 5 LPA</span></div>
  <div class="status-info">2 days ago</div>
  <div class="about_job"><div class="text">Write python</div></div>
</div>"""


def _card(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser").select_one("div.individual_internship")


def test_internshala_card_fields():
    j = js._internshala_card(_card(INTERNSHALA_CARD), ["python"])
    assert (j.title, j.company, j.source) == ("Python Developer", "Acme Pvt Ltd", "internshala")
    assert j.url == "https://internshala.com/job/detail/python-dev-acme"
    assert j.location == "Bangalore, Pune, India"
    assert j.exp_min == 0 and j.level == "junior"
    assert j.job_type == "full-time" and j.tags == ["python"]
    assert j.posted  # "2 days ago" parsed


def test_internshala_card_without_title_link_is_skipped():
    assert js._internshala_card(_card('<div class="individual_internship" internshipid="2"></div>'), []) is None


def test_internshala_listing_follows_category_page(monkeypatch):
    html = f"<html>{INTERNSHALA_CARD}</html>"
    serve(monkeypatch, lambda url, **kw: FakeResp(text=html, url=url))
    jobs = js.internshala("python", pages=1)
    assert [j.title for j in jobs] == ["Python Developer"]


def test_internshala_unknown_slug_redirect_returns_empty(monkeypatch):
    serve(monkeypatch, lambda url, **kw: FakeResp(text="<html></html>", url="https://internshala.com/jobs/"))
    assert js.internshala("zzzz-nonsense", pages=1) == []


# --- keyed APIs ----------------------------------------------------------------------------------

def test_adzuna_requires_keys(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    assert js.adzuna("python") == []


def test_adzuna(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    serve_one(monkeypatch, FakeResp({"results": [{
        "title": "<b>Python</b> Dev", "company": {"display_name": "Theta"},
        "location": {"display_name": "Mumbai"}, "redirect_url": "https://adzuna/1",
        "description": "<p>d</p>", "created": "2026-10-04T00:00:00Z", "contract_time": "full_time",
        "salary_min": 800000, "salary_max": 1200000}]}))
    [j] = js.adzuna("python", ["mumbai"])
    assert (j.title, j.company, j.location, j.source) == ("Python Dev", "Theta", "Mumbai", "adzuna")


def test_jooble_requires_key(monkeypatch):
    monkeypatch.delenv("JOOBLE_API_KEY", raising=False)
    assert js.jooble("python") == []


def test_jooble(monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "k")
    serve_one(monkeypatch, FakeResp({"jobs": [{
        "title": "Dev", "company": "Iota", "location": "Delhi", "link": "https://jooble/1",
        "snippet": "<b>x</b>", "updated": "2026-10-05T00:00:00", "type": "Full-time", "salary": "5 LPA"}]}))
    [j] = js.jooble("dev", ["delhi"])
    assert (j.title, j.company, j.source) == ("Dev", "Iota", "jooble")


def _mcp_reply(structured=None, text=None, error=None, is_error=False, sse=False):
    result = {}
    if structured is not None:
        result["structuredContent"] = structured
    if text is not None:
        result["content"] = [{"type": "text", "text": text}]
    if is_error:
        result["isError"] = True
    msg = {"jsonrpc": "2.0", "id": 1, **({"error": error} if error else {"result": result})}
    if sse:
        return FakeResp(text=f"event: message\ndata: {json.dumps(msg)}\n\n",
                        headers={"content-type": "text/event-stream"})
    return FakeResp(msg, headers={"content-type": "application/json"})


JV_ROW = {"title": "SRE", "company": "Kappa", "location": "Pune", "url": "https://jv/1",
          "employment_type": "full-time", "work_model": "Remote", "salary": {"min": 1, "max": 2, "currency": "INR"}}


@pytest.mark.parametrize("reply", [
    _mcp_reply(structured={"jobs": [JV_ROW]}),
    _mcp_reply(text=json.dumps({"jobs": [JV_ROW]})),             # older servers: JSON inside a text block
    _mcp_reply(structured={"jobs": [JV_ROW]}, sse=True),         # streamed (SSE) transport
])
def test_jobvetta_accepts_every_reply_format(monkeypatch, reply):
    monkeypatch.setenv("JOBVETTA_API_KEY", "k")
    serve_one(monkeypatch, reply)
    [j] = js.jobvetta("sre", ["pune"])
    assert (j.title, j.company, j.source, j.remote) == ("SRE", "Kappa", "jobvetta", True)
    assert j.location == "Pune, India"


def test_jobvetta_skips_non_india_without_calling(monkeypatch):
    monkeypatch.setenv("JOBVETTA_API_KEY", "k")
    serve(monkeypatch, lambda *a, **k: pytest.fail("should not call the API for a non-India location"))
    assert js.jobvetta("sre", ["berlin"]) == []


def test_jobvetta_rate_limited_raises_clear_error(monkeypatch):
    monkeypatch.setenv("JOBVETTA_API_KEY", "k")
    serve_one(monkeypatch, FakeResp(status=429))
    with pytest.raises(RuntimeError, match="daily limit"):
        js.jobvetta("sre", ["pune"])


def test_jobvetta_unparseable_text_returns_empty(monkeypatch):
    monkeypatch.setenv("JOBVETTA_API_KEY", "k")
    serve_one(monkeypatch, _mcp_reply(text="not json at all"))
    assert js.jobvetta("sre", ["pune"]) == []


def test_jobvetta_caches_repeat_search(monkeypatch):
    monkeypatch.setenv("JOBVETTA_API_KEY", "k")
    calls = []

    def handler(url, **kw):
        calls.append(1)
        return _mcp_reply(structured={"jobs": [JV_ROW]})
    serve(monkeypatch, handler)
    js.jobvetta("sre", ["pune"])
    js.jobvetta("sre", ["pune"])
    assert len(calls) == 1  # free key is 50 calls/day - a repeat must come from cache


def test_serpapi(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    serve_one(monkeypatch, FakeResp({"jobs_results": [{
        "title": "ML Engineer", "company_name": "Lambda", "location": "Bengaluru, Karnataka, India",
        "description": "<p>models</p>", "extensions": ["3 days ago", "Full-time"],
        "apply_options": [{"link": "https://apply/1"}]}]}))
    [j] = js.serpapi_jobs("ml engineer", ["bangalore"])
    assert (j.title, j.company, j.source, j.url) == ("ML Engineer", "Lambda", "serpapi", "https://apply/1")
    assert j.job_type == "full-time" and j.posted


def test_serpapi_api_error_returns_empty(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    serve_one(monkeypatch, FakeResp({"error": "Invalid API key"}))
    assert js.serpapi_jobs("x") == []


def test_serpapi_requires_key(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert js.serpapi_jobs("x") == []


def test_metered_source_skips_when_quota_exhausted(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    monkeypatch.setattr(js.quota, "allow", lambda *a, **k: False)
    serve(monkeypatch, lambda *a, **k: pytest.fail("must not call the API once quota is used up"))
    assert js.serpapi_jobs("x") == []


# --- every source: HTTP failure behaviour --------------------------------------------------------

@pytest.mark.parametrize("fn", [js.remotive, js.remoteok, js.jobicy, js.himalayas, js.instahyre])
def test_http_error_raises_so_the_orchestrator_can_record_it(monkeypatch, fn):
    """Sources raise on HTTP errors rather than returning []: search_jobs catches this per source
    and shows it on the Status page. Returning [] here would hide a dead source."""
    serve_one(monkeypatch, FakeResp(status=500))
    with pytest.raises(requests.HTTPError):
        fn("python")


# --- ATS readers (careers.py) --------------------------------------------------------------------

def test_greenhouse(monkeypatch):
    serve_one(monkeypatch, FakeResp({"jobs": [
        {"title": "SRE", "location": {"name": "Remote - India"}, "absolute_url": "https://gh/1",
         "first_published": "2026-10-01T00:00:00Z"},
        {"title": "PM", "location": None, "absolute_url": "https://gh/2", "updated_at": "2026-09-01"}]}))
    a, b = careers.greenhouse("acme", "Acme")
    assert (a["company"], a["title"], a["location"], a["url"]) == ("Acme", "SRE", "Remote - India", "https://gh/1")
    assert a["remote"] is True and a["posted"] == "2026-10-01T00:00:00Z"
    assert b["location"] == "" and b["posted"] == "2026-09-01"  # null location tolerated, falls back to updated_at


def test_lever(monkeypatch):
    serve_one(monkeypatch, FakeResp([{
        "text": "Designer", "categories": {"allLocations": ["Pune", "Remote"], "commitment": "Full-time",
                                           "team": "Design"},
        "hostedUrl": "https://lever/1", "createdAt": 1760000000000, "descriptionPlain": "3+ years of design",
        "workplaceType": "remote", "salaryRange": {"min": 10, "max": 20, "currency": "USD", "interval": "per-year-salary"}}]))
    [j] = careers.lever("acme", "Acme")
    assert j["location"] == "Pune, Remote" and j["remote"] is True and j["department"] == "Design"
    assert j["job_type"] == "Full-time"
    assert j["salary"] == (10, 20, "USD", "salary")
    assert "3+ years" in j["description"]  # fact snippet kept for experience parsing


def test_lever_without_salary(monkeypatch):
    serve_one(monkeypatch, FakeResp([{"text": "Dev", "categories": {"location": "NYC"}}]))
    [j] = careers.lever("acme", "Acme")
    assert j["salary"] is None and j["location"] == "NYC"


def test_ashby_skips_unlisted_and_merges_locations(monkeypatch):
    serve_one(monkeypatch, FakeResp({"jobs": [
        {"title": "Hidden", "isListed": False},
        {"title": "Eng", "location": "London", "secondaryLocations": [{"location": "Berlin"}],
         "jobUrl": "https://ashby/1", "publishedAt": "2026-10-01", "employmentType": "FullTime",
         "isRemote": False, "workplaceType": "Remote", "department": "Eng",
         "compensation": {"compensationTierSummary": "$100K - $150K"}, "descriptionPlain": "5 years"}]}))
    [j] = careers.ashby("acme", "Acme")
    assert j["title"] == "Eng" and j["location"] == "London, Berlin"
    assert j["remote"] is True  # from workplaceType even though isRemote is False
    assert "$100K" in j["description"]


def test_smartrecruiters_paginates(monkeypatch):
    def page(n, start):
        return {"content": [{"id": f"{start + i}", "name": f"Job {start + i}",
                             "location": {"city": "Pune", "country": "in"}, "releasedDate": "2026-10-01"}
                            for i in range(n)]}
    offsets = []

    def handler(url, **kw):
        offsets.append(kw["params"]["offset"])
        return FakeResp(page(100, 0) if kw["params"]["offset"] == 0 else page(5, 100))
    serve(monkeypatch, handler)
    jobs = careers.smartrecruiters("acme", "Acme", max_jobs=300)
    assert len(jobs) == 105 and offsets == [0, 100]
    assert jobs[0]["url"] == "https://jobs.smartrecruiters.com/acme/0"
    assert jobs[0]["location"] == "Pune, in"


def test_fetch_board_applies_india_only(monkeypatch):
    serve_one(monkeypatch, FakeResp({"jobs": [
        {"title": "A", "location": {"name": "Bengaluru, India"}, "absolute_url": "u1"},
        {"title": "B", "location": {"name": "Austin, TX"}, "absolute_url": "u2"}]}))
    jobs = careers.fetch_board({"ats": "greenhouse", "slug": "acme", "name": "Acme", "india_only": True})
    assert [j["title"] for j in jobs] == ["A"]


def test_fetch_board_serves_stale_copy_when_refresh_fails(monkeypatch):
    company = {"ats": "greenhouse", "slug": "acme", "name": "Acme"}
    serve_one(monkeypatch, FakeResp({"jobs": [{"title": "A", "location": {"name": "x"}, "absolute_url": "u"}]}))
    assert len(careers.fetch_board(company)) == 1
    key = ("greenhouse", "acme")
    careers._board_cache[key] = (0, careers._board_cache[key][1])  # expire it
    serve(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert len(careers.fetch_board(company)) == 1  # last good copy, not an error


def test_fetch_board_raises_when_never_loaded_and_down(monkeypatch):
    serve(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with pytest.raises(requests.ConnectionError):
        careers.fetch_board({"ats": "greenhouse", "slug": "nope", "name": "Nope"})
    # and a second call right away doesn't hit the network again (failed-board backoff)
    serve(monkeypatch, lambda *a, **k: pytest.fail("should be in backoff"))
    assert careers.fetch_board({"ats": "greenhouse", "slug": "nope", "name": "Nope"}) == []


# --- Google Custom Search (job_scraper.google_search / _google_item_to_job) -----------------------

def _g(title, link, snippet="", **pagemap):
    return {"title": title, "link": link, "snippet": snippet, "pagemap": pagemap}


def test_google_title_greenhouse_application_shape():
    j = js._google_item_to_job(_g("Job Application for Backend Engineer at Acme Corp",
                                  "https://boards.greenhouse.io/acme/jobs/1", "Build things"))
    assert (j.title, j.company, j.source) == ("Backend Engineer", "Acme Corp", "google")
    assert j.url == "https://boards.greenhouse.io/acme/jobs/1" and "Build things" in j.description


def test_google_title_linkedin_hiring_shape():
    j = js._google_item_to_job(_g("Acme hiring Python Developer in Pune, Maharashtra | LinkedIn",
                                  "https://in.linkedin.com/jobs/view/123"))
    assert (j.company, j.title, j.location) == ("Acme", "Python Developer", "Pune, Maharashtra")


def test_google_dash_title_is_title_then_company_by_default():
    j = js._google_item_to_job(_g("Data Engineer - Globex", "https://www.workable.com/j/1"))
    assert (j.title, j.company) == ("Data Engineer", "Globex")


@pytest.mark.parametrize("link", ["https://jobs.lever.co/globex/1", "https://jobs.ashbyhq.com/globex/1"])
def test_google_dash_title_is_company_then_title_on_lever_and_ashby(link):
    j = js._google_item_to_job(_g("Globex - Data Engineer", link))
    assert (j.title, j.company) == ("Data Engineer", "Globex")


def test_google_company_falls_back_to_slug_in_url():
    j = js._google_item_to_job(_g("Senior Designer", "https://jobs.lever.co/big-widget-co/abc"))
    assert j.company == "Big Widget Co" and j.title == "Senior Designer"


def test_google_jobposting_markup_overrides_title_parsing():
    j = js._google_item_to_job(_g("Job Application for Wrong at Wrong", "https://x.com/j",
                                  jobposting=[{"title": "Right Title", "hiringorganization": "Right Co",
                                               "joblocation": "Mumbai", "dateposted": "2026-10-01"}]))
    assert (j.title, j.company, j.location, j.posted) == ("Right Title", "Right Co", "Mumbai", "2026-10-01")


def test_google_cleans_ellipsis_pipes_and_html():
    j = js._google_item_to_job(_g("Job Application for <b>Dev</b>... at Initech | LinkedIn",
                                  "https://boards.greenhouse.io/initech/1"))
    assert j.title == "Dev" and j.company == "Initech"


def test_google_og_description_is_added_to_description():
    j = js._google_item_to_job(_g("Dev - Acme", "https://x.com/j", "snippet text",
                                  metatags=[{"og:description": "meta text"}]))
    assert "snippet text" in j.description and "meta text" in j.description


@pytest.mark.parametrize("item", [{}, {"title": "", "link": ""}, {"pagemap": None}, {"pagemap": {"metatags": []}}])
def test_google_sparse_or_odd_items_do_not_crash(item):
    j = js._google_item_to_job(item)
    assert j.source == "google"


def _google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")


def test_google_search_requires_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CSE_ID", raising=False)
    serve(monkeypatch, lambda *a, **k: pytest.fail("must not call the API without credentials"))
    assert js.google_search("python", None, ["pune"]) == []


def test_google_search_skips_when_quota_exhausted(monkeypatch):
    _google_env(monkeypatch)
    monkeypatch.setattr(js.quota, "allow", lambda *a, **k: False)
    serve(monkeypatch, lambda *a, **k: pytest.fail("must not call the API once quota is used up"))
    assert js.google_search("python", None, ["pune"]) == []


def test_google_search_builds_query_and_params_for_india(monkeypatch):
    _google_env(monkeypatch)
    seen = []
    serve(monkeypatch, lambda url, **kw: seen.append(kw["params"]) or FakeResp({"items": []}))
    js.google_search("python developer", "senior", ["bangalore"], date_restrict="d7")
    p = seen[0]
    assert '"python developer"' in p["q"] and '"bangalore"' in p["q"] and "senior" in p["q"]
    assert "site:" in p["q"]
    assert p["gl"] == "in"              # Indian location defaults the country
    assert p["dateRestrict"] == "d7" and p["cx"] == "cx" and p["start"] == 1


def test_google_search_mid_level_is_not_added_to_query(monkeypatch):
    _google_env(monkeypatch)
    seen = []
    serve(monkeypatch, lambda url, **kw: seen.append(kw["params"]) or FakeResp({"items": []}))
    js.google_search("python", "mid", ["pune"])
    assert " mid" not in seen[0]["q"]


def test_google_search_paginates_and_caps_results(monkeypatch):
    _google_env(monkeypatch)
    starts = []

    def handler(url, **kw):
        starts.append(kw["params"]["start"])
        return FakeResp({"items": [_g(f"Dev {kw['params']['start']}-{i} - Acme", f"https://x.com/{kw['params']['start']}/{i}")
                                   for i in range(10)]})
    serve(monkeypatch, handler)
    jobs = js.google_search("dev", None, ["pune"], max_results=25)
    assert len(jobs) == 25           # trimmed to max_results
    assert starts == [1, 11, 21]     # 10 per page, stopped once enough


def test_google_search_stops_on_http_error_and_keeps_earlier_pages(monkeypatch):
    _google_env(monkeypatch)

    def handler(url, **kw):
        if kw["params"]["start"] == 1:
            return FakeResp({"items": [_g("Dev - Acme", "https://x.com/1")]})
        return FakeResp(status=429, text="quota exceeded")
    serve(monkeypatch, handler)
    jobs = js.google_search("dev", None, ["pune"])
    assert [j.url for j in jobs] == ["https://x.com/1"]
