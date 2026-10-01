"""Regression tests for the pure parsing/matching logic in job_scraper.py.

Several of these cover real bugs found by hand during development (noted inline) - the point of
this file is that the next one gets caught automatically instead of needing another manual debugging
session. No network access needed: everything here is plain string/data parsing.

Run:  pytest tests/
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_scraper import (
    Job, _fuzzy_key, fmt_exp, fmt_posted, fmt_salary, fmt_type, is_india, iso_from_relative,
    linkedin_company_url, matches_experience, matches_job_type, matches_location, matches_posted,
    matches_role, matches_salary, normalize_job_type, parse_amount, parse_experience_arg,
    parse_experience_args, parse_salary_text, slugify, sort_jobs, window_days,
)


# --- parse_experience_arg ------------------------------------------------------------------------

def test_parse_experience_range():
    assert parse_experience_arg("3-5") == (3, 5)


def test_parse_experience_plus():
    assert parse_experience_arg("5+") == (5, 99)


def test_parse_experience_single_number():
    assert parse_experience_arg("2") == (2, 2)


def test_parse_experience_level_word():
    assert parse_experience_arg("senior") == (5, 10)


def test_parse_experience_none():
    assert parse_experience_arg(None) is None
    assert parse_experience_arg("") is None


def test_parse_experience_bad_value_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_experience_arg("banana")


# --- parse_amount ---------------------------------------------------------------------------------

def test_parse_amount_lpa():
    assert parse_amount("12 LPA") == 1_200_000


def test_parse_amount_k():
    assert parse_amount("80k") == 80_000


def test_parse_amount_crore():
    assert parse_amount("1.5 cr") == 15_000_000


def test_parse_amount_plain_number():
    assert parse_amount("100000") == 100_000


def test_parse_amount_with_commas():
    assert parse_amount("1,200,000") == 1_200_000


def test_parse_amount_none():
    assert parse_amount(None) is None
    assert parse_amount("") is None


# --- parse_salary_text -----------------------------------------------------------------------------

def test_salary_dollar_range():
    assert parse_salary_text("$90k - $105k", "") == (90_000.0, 105_000.0, "USD")


def test_salary_rupee_range_with_period():
    lo, hi, cur = parse_salary_text("₹ 6,00,000 - 9,50,000 /year", "")
    assert (lo, hi, cur) == (600_000.0, 950_000.0, "INR")


def test_salary_lpa_shorthand():
    assert parse_salary_text("12-18 LPA", "") == (1_200_000.0, 1_800_000.0, "INR")


def test_salary_hourly_no_unit_is_annualized():
    lo, hi, cur = parse_salary_text("$50-75/hour", "")
    assert cur == "USD" and lo > 100_000  # annualized, not left as 50-75


def test_salary_plain_number_range_not_mistaken_for_years():
    # "3-5" on its own (e.g. an experience range) must NOT be read as a salary
    assert parse_salary_text("3-5 years experience required", "") is None


def test_salary_single_figure_fallback():
    # regression: a one-sided salary used to render as an unhelpful "?" via fmt_salary
    job = Job(title="x", salary_min=500_000, salary_max=None, salary_currency="INR")
    assert "?" not in fmt_salary(job)


# --- role / location matching -----------------------------------------------------------------

def test_matches_role_synonym():
    # "developer" query should match a job titled "... Engineer" via the synonym group
    job = Job(title="Senior Python Engineer")
    assert matches_role(job, "python developer")


def test_matches_role_requires_all_words():
    job = Job(title="Python Developer")
    assert not matches_role(job, "python developer intern")


def test_matches_role_empty_query_matches_anything():
    assert matches_role(Job(title="Anything"), "")


def test_matches_location_bangalore_bengaluru_alias():
    job = Job(title="x", location="Bengaluru, Karnataka, India")
    assert matches_location(job, ["bangalore"])


def test_matches_location_remote():
    job = Job(title="x", location="Worldwide (Remote)")
    assert matches_location(job, ["remote"])


def test_matches_location_no_filter_matches_anything():
    assert matches_location(Job(title="x", location="Nowhere"), [])


def test_matches_location_generic_india_not_confused_with_indiana():
    # regression: an early version of the India location matcher also matched US "Indiana"/"Indianapolis"
    job = Job(title="x", location="Indianapolis, IN")
    assert not matches_location(job, ["india"])


# --- experience / salary / job-type / date matching ---------------------------------------------

def test_matches_experience_within_range():
    job = Job(title="x", exp_min=3, exp_max=5)
    assert matches_experience(job, [(2, 6)], strict=False)


def test_matches_experience_outside_range():
    job = Job(title="x", exp_min=8, exp_max=10)
    assert not matches_experience(job, [(0, 2)], strict=False)


def test_matches_experience_unknown_kept_unless_strict():
    job = Job(title="x")  # no exp info at all
    assert matches_experience(job, [(2, 5)], strict=False)
    assert not matches_experience(job, [(2, 5)], strict=True)


def test_matches_experience_multiple_ranges_is_or_not_and():
    job = Job(title="x", exp_min=0, exp_max=1)
    assert matches_experience(job, [(0, 1), (5, 99)], strict=False)  # matches the first range
    assert not matches_experience(job, [(3, 4), (5, 99)], strict=False)  # matches neither


def test_parse_experience_args_multiple_values():
    assert parse_experience_args(["0-1", "5+"]) == [(0, 1), (5, 99)]
    assert parse_experience_args("0-1,5+") == [(0, 1), (5, 99)]
    assert parse_experience_args(None) == []
    assert parse_experience_args([]) == []


def test_matches_salary_min_threshold():
    job = Job(title="x", salary_min=800_000, salary_max=1_200_000, salary_currency="INR")
    assert matches_salary(job, 1_000_000, None, "INR", salary_only=False)
    assert not matches_salary(job, 1_500_000, None, "INR", salary_only=False)


def test_matches_salary_unknown_kept_unless_salary_only():
    job = Job(title="x")
    assert matches_salary(job, 1_000_000, None, "INR", salary_only=False)
    assert not matches_salary(job, 1_000_000, None, "INR", salary_only=True)


def test_normalize_job_type_variants():
    assert normalize_job_type("Full Time") == "full-time"
    assert normalize_job_type("full_time") == "full-time"
    assert normalize_job_type("Intern") == "internship"
    assert normalize_job_type(["Experienced", "Permanent", "Full time"]) == "full-time"


def test_matches_job_type_internship_and_remote():
    job = Job(title="x", job_type="internship", remote=True)
    assert matches_job_type(job, ["internship", "remote"])
    assert not matches_job_type(job, ["full-time", "remote"])


def test_matches_job_type_untyped_counts_as_full_time():
    job = Job(title="x")
    assert matches_job_type(job, ["full-time"])


def test_window_days():
    assert window_days("d7") == 7
    assert window_days("w2") == 14
    assert window_days("m1") == 30
    assert window_days(None) is None


def test_matches_posted_within_window():
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert matches_posted(Job(title="x", posted=recent), 1)
    assert not matches_posted(Job(title="x", posted=old), 1)


def test_matches_posted_undated_dropped_once_window_chosen():
    job = Job(title="x", posted="")
    assert matches_posted(job, None)  # no window chosen: keep
    assert not matches_posted(job, 7)  # window chosen: drop undated


def test_iso_from_relative():
    assert iso_from_relative("2 days ago") != ""
    assert iso_from_relative("nonsense") == ""


# --- fuzzy cross-source dedup key ---------------------------------------------------------------

def test_fuzzy_key_ignores_company_suffix_and_city_alias():
    j1 = Job(title="Backend Engineer", company="Acme Pvt Ltd", location="Bengaluru, India")
    j2 = Job(title="Backend Engineer", company="Acme Technologies", location="Bangalore")
    assert _fuzzy_key(j1) == _fuzzy_key(j2)


def test_fuzzy_key_generic_india_not_merged_with_specific_city():
    # regression: LOCATION_ALIASES["india"] expands to every Indian city for matching purposes -
    # using it to canonicalize would wrongly fold a bare "India" job into one arbitrary city
    j1 = Job(title="Backend Engineer", company="Acme", location="Bangalore")
    j2 = Job(title="Backend Engineer", company="Acme", location="India")
    assert _fuzzy_key(j1) != _fuzzy_key(j2)


def test_fuzzy_key_none_without_title_or_company():
    assert _fuzzy_key(Job(title="", company="Acme")) is None
    assert _fuzzy_key(Job(title="Engineer", company="")) is None


# --- sorting / formatting --------------------------------------------------------------------

def test_sort_jobs_salary_desc_ranks_by_top_of_range():
    high = Job(title="a", salary_min=1_000_000, salary_max=2_000_000, salary_currency="INR")
    low = Job(title="b", salary_min=500_000, salary_max=800_000, salary_currency="INR")
    unknown = Job(title="c")
    result = sort_jobs([unknown, low, high], "salary_desc", "INR")
    assert result == [high, low, unknown]


def test_sort_jobs_relevance_is_a_no_op():
    jobs = [Job(title="a"), Job(title="b")]
    assert sort_jobs(jobs, "relevance", "INR") == jobs


def test_fmt_exp_range_and_plus():
    assert fmt_exp(Job(title="x", exp_min=2, exp_max=5)) == "2-5 yrs"
    assert fmt_exp(Job(title="x", exp_min=5, exp_max=99)) == "5+ yrs"
    assert fmt_exp(Job(title="x", level="senior")) == "senior"
    assert fmt_exp(Job(title="x")) == "-"


def test_fmt_type_combines_kind_and_remote():
    job = Job(title="x", job_type="internship", remote=True)
    assert "Internship" in fmt_type(job) and "Remote" in fmt_type(job)
    assert fmt_type(Job(title="x")) == "-"


def test_is_india():
    assert is_india(["bangalore"])
    assert is_india(["india"])
    assert not is_india(["berlin"])
    assert not is_india(["remote"])


# --- linkedin_company_url -------------------------------------------------------------------------

def test_linkedin_url_strips_legal_suffixes():
    assert linkedin_company_url("Infosys Limited") == "https://www.linkedin.com/company/infosys/"
    assert linkedin_company_url("Acme Corp") == "https://www.linkedin.com/company/acme/"
    assert linkedin_company_url("Example Pvt Ltd") == "https://www.linkedin.com/company/example/"


def test_linkedin_url_slugifies_the_rest():
    assert linkedin_company_url("Mahindra & Mahindra Ltd") == "https://www.linkedin.com/company/mahindra-mahindra/"


def test_linkedin_url_never_empty_even_for_an_all_suffix_name():
    assert linkedin_company_url("Ltd") == "https://www.linkedin.com/company/ltd/"


def test_slugify():
    assert slugify("Python Developer!") == "python-developer"
    assert slugify("  Multi   Space  ") == "multi-space"
