# Job Scraper

Searches jobs worldwide and filters them by **role**, **experience** and **location**.

| Source | Key needed | Coverage |
|---|---|---|
| `google` – Google Custom Search JSON API | Yes (free, 100 queries/day) | Greenhouse, Lever, Workday, Ashby, SmartRecruiters, Workable, LinkedIn, Wellfound job pages, worldwide. **When the location is in India** it searches Naukri, Foundit, Instahyre, Cutshort, iimjobs, Hirist, Internshala, Shine, Indeed India and LinkedIn instead |
| `careers` | No | Company careers pages of the 999 companies in `companies.json` (~40k jobs): Indian companies (Razorpay, Swiggy, CRED, Paytm, Freshworks…), global tech companies, **266 US-origin companies with offices in India** (Walmart, Amazon, JPMorgan, Citi, Accenture, NVIDIA, Salesforce, Cisco, Texas Instruments, Medtronic, Wells Fargo…, only their India jobs kept), and **598 Y Combinator startups** with an open Greenhouse/Lever/Ashby board |
| `instahyre` | No | India tech jobs (experience filtered server-side) |
| `cutshort` | No | India startup jobs, with experience ranges (pages exist per skill, e.g. "python", "react") |
| `internshala` | No | India fresher / early-career jobs |
| `adzuna` | Yes (free) | India + 15 other countries |
| `jooble` | Yes (free) | India + ~70 other countries |
| `jobvetta` | Yes (free, 50 calls/day) | India jobs checked against employers' own sites, via Jobvetta's MCP server. One call per search (up to 10 jobs), repeat searches reused for 30 min; skipped for non-Indian locations |
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

## CLI

```powershell
python job_scraper.py --role "data engineer" --experience 3-5 --location "india,remote"
python job_scraper.py --role "react developer" --experience senior --location berlin --country-code de --out jobs.csv
python job_scraper.py --role "backend developer" --location india --min-salary "15 LPA" --salary-only
python job_scraper.py --role python --location india --job-type internship,remote
python job_scraper.py --role "product manager" --location "london" --sources google --max-google 100 --date w2
```

| Option | Meaning |
|---|---|
| `--role` | Every word (or a synonym, e.g. developer ≈ engineer) must be in the job title |
| `--experience` | `3-5`, `5+`, `2`, or a level: intern, entry, junior, mid, senior, lead, staff, principal |
| `--location` | Comma-separated cities/countries/`remote` |
| `--sources` | Subset of `google,careers,instahyre,cutshort,internshala,adzuna,jooble,jobvetta,himalayas,remotive,arbeitnow,remoteok,jobicy` |
| `--max-google` | Google results (10 per API call; max 100) |
| `--country-code` | Google `gl` boost, e.g. `in`, `us`, `de` |
| `--date` | Posted within: `d1` (24 hours), `d7`, `w2`, `m1`, `m3`; default any time. Applies to every source |
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

## Posted date

"Posted within" filters every source, not only Google. Dates come from the source (exact timestamps for most;
Internshala's "2 days ago" is converted; Cutshort's is read from the job ID). **Instahyre publishes no dates**, so
its jobs, and any other undated job, drop out once a time window is chosen.
