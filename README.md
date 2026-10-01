# Job Scraper

Searches jobs worldwide and filters them by **role**, **experience** and **location**.

| Source | Key needed | Coverage |
|---|---|---|
| `google` – Google Custom Search JSON API | Yes (free, 100 queries/day) | Greenhouse, Lever, Workday, Ashby, SmartRecruiters, Workable, LinkedIn, Wellfound job pages, worldwide. **When the location is in India** it searches Naukri, Foundit, Instahyre, Cutshort, iimjobs, Hirist, Internshala, Shine, Indeed India and LinkedIn instead |
| `careers` | No | Company careers pages of the 1000+ companies in `companies.json` (this number changes daily - see below): Indian companies (Razorpay, Swiggy, CRED, Paytm, Freshworks…), global tech companies, US-origin companies with offices in India (Walmart, Amazon, JPMorgan, Citi, Accenture, NVIDIA, Salesforce, Cisco, Texas Instruments, Medtronic, Wells Fargo…, only their India jobs kept), and Y Combinator startups with an open Greenhouse/Lever/Ashby board |
| `instahyre` | No | India tech jobs (experience filtered server-side) |
| `cutshort` | No | India startup jobs, with experience ranges (pages exist per skill, e.g. "python", "react") |
| `internshala` | No | India fresher / early-career jobs |
| `adzuna` | Yes (free) | India + 15 other countries |
| `jooble` | Yes (free) | India + ~70 other countries |
| `jobvetta` | Yes (free, 50 calls/day) | India jobs checked against employers' own sites, via Jobvetta's MCP server. One call per search (up to 10 jobs), repeat searches reused for 30 min; skipped for non-Indian locations |
| `serpapi` | Yes (free, 100 searches/**month**) | Google Jobs results worldwide, via [SerpApi](https://serpapi.com). One call per search, no pagination - the free plan's monthly cap is much tighter than the other sources' daily ones, so `SERPAPI_DAILY_LIMIT` (default 3/day) spreads it out instead of letting one busy day burn the whole month |
| `himalayas` | No | Remote jobs; limited to India-eligible ones when the location is in India |
| `remotive`, `remoteok`, `jobicy` | No | Remote jobs worldwide |
| `arbeitnow` | No | Europe (mostly Germany) + remote |

Naukri isn't included directly: its API requires reCAPTCHA. Its listings are reached through Google instead.

Location matching understands aliases: `india` matches Indian cities, `bangalore` ↔ `bengaluru`,
`gurgaon` ↔ `gurugram`, `delhi`/`ncr` include Noida and Gurgaon.

## Setup

```powershell
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then fill in the two values
```

Google keys (free):
1. **API key** – https://console.cloud.google.com → enable **Custom Search API** → Credentials → Create API key.
2. **Search engine ID (cx)** – https://programmablesearchengine.google.com → Add → "Search the entire web" → copy the ID.

Without keys the scraper still runs on the keyless sources.

**Running the tests:**
```powershell
.\.venv\Scripts\pip install -r requirements-dev.txt
pytest tests/
```

## CLI

```powershell
python job_scraper.py --role "data engineer" --experience 3-5 --location "india,remote"
python job_scraper.py --role "react developer" --experience senior --location berlin --country-code de --out jobs.csv
python job_scraper.py --role "backend developer" --location india --min-salary "15 LPA" --salary-only
python job_scraper.py --role python --location india --job-type internship,remote
python job_scraper.py --role "product manager" --location "london" --sources google --max-google 100 --date d7
```

| Option | Meaning |
|---|---|
| `--role` | Every word (or a synonym, e.g. developer ≈ engineer) must be in the job title |
| `--experience` | `3-5`, `5+`, `2`, or a level: intern, entry, junior, mid, senior, lead, staff, principal. Comma-separated for multiple (matches ANY of them), e.g. `0-1,5+` |
| `--location` | Comma-separated cities/countries/`remote` |
| `--sources` | Subset of `google,careers,instahyre,cutshort,internshala,adzuna,jooble,jobvetta,serpapi,himalayas,remotive,arbeitnow,remoteok,jobicy` |
| `--max-google` | Google results (10 per API call; max 100) |
| `--country-code` | Google `gl` boost, e.g. `in`, `us`, `de` |
| `--date` | Posted within: `d1` (24 hours), `d2`, `d3`, `d7`; web UI defaults to `d1`, CLI defaults to any time. Applies to every source |
| `--strict` | Drop jobs whose experience can't be detected |
| `--min-salary` / `--max-salary` | Annual salary: `1200000`, `"12 LPA"`, `12L`, `80k`, `1.5cr` |
| `--currency` | Currency of the salary filter (default INR for Indian locations, else USD). The web UI always uses the default |
| `--salary-only` | Drop jobs that don't list a salary |
| `--job-type` | `full-time`, `internship`, `remote`, comma-separated. Full-time/internship are alternatives; remote narrows them (`internship,remote` = remote internships) |
| `--sort` | `relevance` (default), `salary_desc`, `salary_asc` |
| `--out` | Save to `.csv` or `.json` |

## Web UI

```powershell
python app.py   # http://127.0.0.1:5000
```

Debug mode (auto-reload on code changes) is off by default because it runs a second copy of the app and roughly
doubles memory use. Turn it on with `$env:JOB_SCRAPER_DEBUG = "1"` before `python app.py`.

## Deploying (Streamlit Community Cloud)

`app.py` is a Flask app and **cannot run on Streamlit Community Cloud** - that platform only knows how to execute
a Streamlit script (`streamlit run <file>`), not something that starts its own server with `app.run()`. Use
`streamlit_app.py` instead: the same search UI, rebuilt with Streamlit widgets on top of the same
`job_scraper.py` / `careers.py` logic.

```powershell
streamlit run streamlit_app.py   # local run, http://localhost:8501
```

To deploy: on [share.streamlit.io](https://share.streamlit.io), point the app's **Main file path** at
`streamlit_app.py` (not `app.py`). If an app was already created pointing at `app.py`, open its settings and
change the main file path, then reboot the app.

**Secrets:** there's no `.env` on Streamlit Cloud (it's gitignored, never deployed). Add any keys you have under
the app's **Settings → Secrets**, as TOML:
```toml
GOOGLE_API_KEY = "..."
GOOGLE_CSE_ID = "..."
ADZUNA_APP_ID = "..."
ADZUNA_APP_KEY = "..."
JOOBLE_API_KEY = "..."
JOBVETTA_API_KEY = "..."
SERPAPI_API_KEY = "..."
```
`streamlit_app.py` copies these into the environment on startup, since `job_scraper.py` reads them with
`os.getenv`. Sources whose key is missing are skipped, same as running locally without them.

**Careers pages load in the background,** the same way `app.py` does it: a background thread refreshes all
~1000 company boards on a loop, shared across every visitor (`st.cache_resource` makes it a once-per-app-instance
singleton, not once-per-visitor). The first search after a cold start shows a "still loading" notice and returns
whatever's loaded so far, rather than blocking for the full ~10-13 minutes.

**Memory:** Streamlit Community Cloud's free tier caps apps at about 1 GB of RAM. With ~1000 companies and
~40k careers jobs cached, this is a real constraint - if the deployed app restarts unexpectedly or looks stuck,
that's the likely cause. Trimming `companies.json` (or dropping `careers` from the default source list) reduces
memory use if this happens. See **Low-memory mode** below for a built-in knob.

## Reliability, quality, and extra features

A public deployment shares its resources (API quotas, memory) across every visitor, so these were added once
this app moved from a personal tool to something anyone could open:

**API quota protection (`quota.py`).** Jobvetta (50 calls/day), Google Custom Search (100/day), Adzuna, and
Jooble all have daily free-tier caps. Once anyone can visit the deployed link, a handful of searches could burn
through Jobvetta's quota in minutes and leave it dead for everyone else that day. Every metered source now
checks its own remaining quota before calling out, and skips itself (falling back to the other sources) once
the day's quota is used - same as it already does when a key is simply missing. There's also an app-wide search
rate limit (`MAX_SEARCHES_PER_MINUTE`, default 20): if the app is being searched faster than that, metered
sources are skipped for that search rather than let a traffic burst exhaust the daily cap. Both are visible on
the **Status** page/tab. Override a limit with an env var, e.g. `JOBVETTA_DAILY_LIMIT=30`.

**Low-memory mode (`LOW_MEMORY=1`).** The handful of large multinationals on Workday, SmartRecruiters, Oracle,
and Amazon are what push total memory up (each can return hundreds to thousands of jobs). Setting `LOW_MEMORY=1`
lowers the per-board cap on just those sources (e.g. Workday from 100 jobs/company to 40), trading some depth on
the biggest employers for a meaningfully smaller memory footprint, without dropping any company outright.

**Automated tests (`tests/`, run with `pytest tests/`).** The suite covers the parsing/matching logic (salary,
experience, role and location matching, job-type detection, date windows, sorting), the saved-search store and
quota tracker, and the discover/prune company-discovery pipeline - all offline, no network needed. Several
encode real bugs found by hand during
development (a role-matching regression, a India/Indiana location mixup, a salary formatting edge case) so the
next one gets caught automatically instead of needing another manual debugging session. A GitHub Actions
workflow (`.github/workflows/tests.yml`) runs them on every push.

**Cross-source duplicate detection.** The same job can appear from more than one source - a company's own
careers page and a job board that also indexed it. Beyond exact URL/title/company/location matches, jobs are
now also compared on a normalized (title, company, city) key that ignores company suffixes ("Pvt Ltd",
"Technologies", ...), punctuation, and known city aliases (Bangalore/Bengaluru, Gurgaon/Gurugram), so near-
duplicate postings from different sources collapse into one result.

**Remaining coverage gaps.** Most Indian companies still aren't reachable: they run Keka, Darwinbox, or Zoho
Recruit, none of which expose a public API - their job listings are rendered client-side with no feed to read,
and adding a headless browser to work around that would be a poor trade against the memory goal above. Large
global employers not already in `companies.json` (Google, Microsoft, Apple, Meta, Uber, ...) are reachable only
through the Google Custom Search source, which needs `GOOGLE_API_KEY` / `GOOGLE_CSE_ID` set.

**Saved searches and alerts (`store.py`, `alerts.py`).** Below a search's results, "🔔 Save & alert me" stores
the search criteria plus an optional webhook URL (a Slack "Incoming Webhook" or Discord channel webhook both
work as-is, or any URL of your own that accepts a JSON POST); a background thread re-runs every saved search
every 30 minutes and posts any newly-found jobs to its webhook. Private to your own browser (an anonymous id in
a cookie for the Flask app, in session state for the Streamlit app - no login). Manage saved searches (check
now / delete) under "🔔 Saved searches". This state lives in a local SQLite file (`job_scraper.db`) - on most
free hosts that's ephemeral across a redeploy, which is an acceptable trade-off for alerts.

**Pagination.** Results are paginated at 50 per page rather than rendered as one unbounded table, which was
both a usability problem and, at large result counts, a real rendering cost.

**JSON API (Flask only).** `/api/search` takes the same query parameters as the search page and returns JSON
(`{"count": ..., "currency": ..., "jobs": [...]}`) for programmatic use. Not available on the Streamlit
deployment - Streamlit doesn't support arbitrary custom routes the way Flask does.

**Status page/tab.** Shows careers-page load progress, boards currently failing, each metered source's daily
quota usage, and per-source fetch counts/errors for the running process - so a quiet failure (a source silently
returning 0 for a while) is visible without digging through logs. Flask: `/status`. Streamlit: the "⚙️ Status"
tab.

## How experience is detected

Years are parsed from the title/description (`3+ years`, `2-4 yrs`, ...); otherwise the level is inferred
from the title (Junior, Senior, Lead, Principal...). Jobs with no signal are kept unless `--strict`.
Google results only have the search snippet, so experience/location detection there is best-effort.

## How salary is handled

Salaries come from structured fields (Cutshort, Himalayas, Jobicy, RemoteOK, Adzuna) or are parsed from text
(`$90k - $105k`, `₹ 6,00,000 - 9,50,000 /year`, `12-18 LPA`, `$50-75/hour`). Everything is converted to an
**annual** amount; hourly/monthly pay is multiplied up (2080 h / 12 months). Different currencies are compared using
live rates from open.er-api.com (free, no key; falls back to built-in rates offline).

A job passes `--min-salary` if the top of its range reaches it, and `--max-salary` if the bottom is below it.
Most listings (e.g. all of Instahyre and Arbeitnow) don't show pay, so unknown-salary jobs are kept unless
`--salary-only`. Salaries that Cutshort employers marked hidden are not shown.

Sorting by salary compares across currencies (converted to the filter currency). High→low ranks by the top of each
range, low→high by the bottom; jobs without a salary always go last. In the web UI use the **Sort** dropdown or click
the **Salary / yr** header; re-sorting reuses the cached results, and the CSV download keeps the chosen order.

## Job type

Taken from each source's own field where it has one (Remotive, Arbeitnow, Jobicy, Himalayas, Cutshort, Adzuna, Jooble),
otherwise inferred from the title ("Intern", "Trainee", "Part-time", "Contract"). Jobs with no stated type count as
full-time. Remote comes from the source (Remotive/RemoteOK/Jobicy/Himalayas are all remote; Cutshort `remoteType`)
or from "Remote"/"Work from home" in the location. Selecting **internship** also pulls Internshala's internship
listings and switches Instahyre to its internship search.

## Company careers pages (`careers`)

Most companies' careers pages run on an applicant-tracking system with a free public job-board API. `careers.py`
reads the boards listed in `companies.json` directly, so jobs come from the company itself. Supported systems:
Greenhouse, Lever, Ashby, SmartRecruiters, **Workday** (India jobs only, newest 100 per company), **Oracle Recruiting
Cloud** (JPMorgan, Oracle, Texas Instruments) and **amazon.jobs**. Companies marked `"india_only": true` (US companies
on the first four systems) keep only their India jobs, so their global boards don't use memory.

To add a Workday company, put an entry like this in `companies.json`, with the tenant, `wdN` and site name from its
careers URL (`https://walmart.wd504.myworkdayjobs.com/WalmartExternal`):
`{"name": "Walmart", "ats": "workday", "slug": "walmart/wd504/WalmartExternal"}`

```powershell
python careers.py list                               # companies being searched
python careers.py add "Postman" "Zerodha" "Notion"    # finds which ATS each uses and adds it
python careers.py remove paytm
```

There is no public list of every company's careers page, so only the companies in `companies.json` are read
directly. Careers pages on other systems (Workday, custom sites) are only reached through Google search.
Downloading everything takes ~9 minutes (Workday returns 20 jobs per request): the web app does it in the background
at startup and refreshes hourly; searches meanwhile use whatever has loaded, with a notice. On the command line, use
`--sources` without `careers` for quick searches, since each run downloads all boards.

### Keeping `companies.json` growing (`discover.py`)

`companies.json` is otherwise a frozen snapshot: a company with no jobs today that opens a board next month would
never be noticed, since the app only ever re-fetches boards already on the list. `discover.py` is the other half -
it checks two candidate pools for ones that have *newly* opened a Greenhouse/Lever/Ashby/SmartRecruiters board,
and adds them:

- **YC** (`yc_candidates.json`, ~6,270 companies) - Y Combinator's full company directory. Startups here are heavy
  adopters of exactly the ATS platforms this app reads, so this pool has a good hit rate.
- **NSE/BSE** (`nse_candidates.json`, ~5,460 companies) - every stock listed on 5paisa (https://www.5paisa.com/stocks/all),
  i.e. essentially every Indian publicly-listed company. A much bigger, much lower-hit-rate pool - most large listed
  Indian corporates use in-house or enterprise HR systems this app doesn't read - but a real fraction do use a
  supported ATS. Official display names come from NSE's own symbol list (`nse_names.json`); BSE-only tickers not in
  that list fall back to a title-cased version of the slug.

This is deliberately **not** part of the live app: checking thousands of candidates takes 1-2+ hours, far too slow
and too much load for an hourly refresh or a visitor's request to wait on. Instead, `.github/workflows/discover-
companies.yml` runs it on a schedule (weekly, Sundays) via GitHub Actions, and commits anything new straight to
`companies.json` - which Streamlit Community Cloud then picks up on its automatic redeploy. Run it manually too:

```powershell
python discover.py                       # check all untracked candidates in both pools
python discover.py --refresh-candidates  # also re-fetch both candidate lists from source first
python discover.py --sources yc          # only check one pool (or --sources nse)
python discover.py --limit 500           # check only the first 500 untracked candidates per pool, for a quicker test run
```

**Collision safety.** The same literal slug can belong to a completely unrelated company on the same ATS (e.g.
Bird Rides' Greenhouse board isn't YC's Bird, a messaging company). `yc_excluded.json` / `nse_excluded.json` are
manually-curated lists of confirmed collisions found this way, so they're never re-added. Short slugs (under 5
characters) are where this happens most for YC - common English words are more likely to already be claimed by
someone else - so a new hit that short is written to `yc_needs_review.json` for a human to check, instead of
being auto-added. A hit is also skipped outright if nothing on the board was posted in the last ~400 days -
probing NSE tickers surfaced a real case of this: a single 2022 SmartRecruiters posting for an unrelated small
company that happened to share a ticker's slug, long since abandoned.

**The NSE/BSE pool never auto-adds anything.** A first real run found that ticker-based slug guessing has a much
higher collision rate than YC's own slugs: `tcs`, `indigo`, `metropolis`, `clara`, `campus`, `karbon`, `bluestone`,
and `sona` all matched a real, currently-hiring board on Greenhouse/Ashby/Lever - none of them the actual NSE-listed
company (e.g. `tcs` was a UK nursing-staffing agency, `indigo` a US company hiring in San Francisco, neither
related to Tata Consultancy Services or IndiGo Airlines). Ticker symbols are short, common words that unrelated
global companies also pick as their ATS slug far more often than YC's own brand names collide. So for this pool,
every hit additionally needs at least one India-based posting to even be considered (a real NSE-listed company
should have some; lacking that, it's discarded outright, no review needed) - and even then it only ever goes to
`nse_needs_review.json` for a human to confirm before manually adding it to `companies.json`, never straight in.

### Removing companies that stopped hiring (`prune.py`)

The mirror image of `discover.py`: nothing above ever *removes* a company, so one that's stopped hiring would sit
in `companies.json` forever, costing an hourly fetch for nothing. `prune.py` rechecks every company already
tracked, and any with **no jobs for 3 consecutive daily runs** is moved out of `companies.json` into
`pruned_companies.json`. `discover.py` rechecks that file every run too (alongside both candidate pools), so a
company that starts hiring again later is added straight back automatically - nothing is lost, just set aside
while it isn't useful. Runs daily via `.github/workflows/prune-companies.yml`.

A single empty day isn't enough to remove a company (a network hiccup or a one-off ATS error looks the same as a
closed board from here) - `prune_state.json` tracks the current consecutive-empty streak per company, resetting
to zero the moment jobs reappear.

```powershell
python prune.py                    # check all companies, remove those past the threshold
python prune.py --threshold 5      # require 5 consecutive empty days instead of the default 3
```

Together, `discover.py` (weekly) and `prune.py` (daily) mean `companies.json` always reflects companies with a
*currently* live board - not a frozen snapshot from whenever they were first added, and not cluttered with ones
that have gone quiet.

## Posted date

"Posted within" filters every source, not only Google. Dates come from the source (exact timestamps for most;
Internshala's "2 days ago" is converted; Cutshort's is read from the job ID). **Instahyre publishes no dates**, so
its jobs, and any other undated job, drop out once a time window is chosen.
