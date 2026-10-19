# Backlink Builder

A local backlink-management website for Android (Termux). Enter your website, discover candidate
pages, assess them transparently, submit only where it is explicitly permitted, track results, and
verify which backlinks actually exist.

Runs at **http://127.0.0.1:8080**. Python 3 + Flask + SQLite + Requests + BeautifulSoup. No Node.js,
no paid APIs, no external server.

## Install on Termux

```sh
pkg update
pkg install python
termux-setup-storage          # only if you keep the project in shared storage
```

Copy/unzip the project, then:

```sh
cd ~/backlink-tool            # or: cd /storage/emulated/0/backlink-tool
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8080` in your phone browser.

> **Tip:** SQLite can be unreliable on Android shared storage (`/storage/emulated/0`). If you see
> "database is locked" errors, move the folder into Termux's home (`cp -r /storage/emulated/0/backlink-tool ~/`)
> and run it from there. The app avoids WAL mode so it works in both places, but home is the safer choice.

Unzip without `unzip`: `python -m zipfile -e backlink-tool.zip .`

### Stop / restart
* Stop: press **Ctrl+C** in the Termux session (or swipe the session closed).
* Restart: `python app.py` again. Your data (`database/backlinks.db`) is kept.
* Keep it alive with the screen off: run `termux-wake-lock` first.
* Check your setup any time: `python main.py --check`
* Run the tests: `python -m unittest discover -s tests -v`

## The workflow

1. **Dashboard** → enter your website (+ optional name, description, category, keywords, languages,
   countries, email, preferred opportunity types) → *Save website*.
2. **Start discovery.** Sources that are switched on are searched, new pages are saved immediately, then each is assessed.
3. Each opportunity lands in one bucket: **Eligible**, **Needs review**, **Login required**,
   **CAPTCHA**, **Outreach**, or **Rejected** (plus *Not assessed* until checked).
4. On **Opportunities**: open *Details* to see the score breakdown, preview a submission, approve eligible sites,
   then *Start approved submissions*. For everything else you get a direct link, the prepared details,
   and (for outreach) a draft message.
5. Every successful-looking submission is stored as a **pending** backlink and re-checked later.
   On **Backlinks**, only rows marked *Live* were actually found on the page.

## Discovery sources (what really exists)

| Source | Needs | Status |
|---|---|---|
| Starter list `data/sources.json` | nothing | Works offline; each entry is fetched and assessed. Many are login-only and are correctly classed so. |
| GitHub public search (awesome-style lists) | nothing (optional `GITHUB_TOKEN` env var for higher rate limit) | Implemented; parsing tested with mock data, **not tested against live GitHub from the build sandbox (no internet)**. |
| SearXNG instance (broad web search) | a public instance URL with JSON output enabled (Settings) | Implemented; **untested against a live instance**. Without it there is no general web search. |
| Reference pages you paste | nothing | Page + its outbound links become candidates. |
| TXT / CSV import, manual add | nothing | Tested. |

The app does **not** search "the whole internet". Google/Bing scraping is deliberately not included.
Localhost SearXNG is rejected by the SSRF guard on purpose.

Opportunity categories covered: directories, profiles, open-source/GitHub/GitLab pages, resource pages,
industry blogs, guest posts, broken-link replacement (dead-link scan of resource pages, HTTP 404/410 only),
community resources, and submission sites.

## Quality assessment

Each factor is labelled **Verified** (read from the page) or **Estimate** (keyword-overlap heuristic).
Score is 0-100 and explicitly *not* Domain Authority/Rating. No traffic, authority or index numbers are
invented; `.edu`/`.gov` get no bonus. Indexing can only be checked for a `noindex` tag; actual search-index
status is reported as unknown.

## Submission engine: when it will and won't submit

Automatic submission happens only if **all** of these hold: the domain isn't `manual_only` in
`data/submissions.json`, the opportunity is *eligible* and **approved by you**, robots.txt allows the page and form path,
no CAPTCHA, no login/registration, no text restricting automation, the form posts to the same site, every required field
can be filled from your profile, no consent checkbox is required (only you can accept terms), honeypot fields stay empty,
and the attempt limit isn't reached. Comment forms, login, search and newsletter forms are never used.

Outcomes: submitted, awaiting approval, pending verification, CAPTCHA required, login required, manual action required,
rejected, failed, already submitted. An HTTP 200 is never treated as proof of a link; the best result is *submitted*
and the backlink stays **Pending** until the verifier finds it.

## Backlink verification

Fetches the source page politely (robots.txt, per-domain delay, retries for 429/5xx/timeouts), follows redirects,
looks for links to your domain, records anchor text, `nofollow`/`ugc`/`sponsored`, first-seen and last-checked times,
and flags links that disappeared. States: Live, Live (nofollow/sponsored/ugc), Redirected, Not found,
Access restricted, Temporary failure, Inconclusive (e.g. JavaScript-rendered pages), plus Pending (never checked).
*Redirected* means the source page moved elsewhere and no link was found there; if the link is found after a redirect it's
Live and the redirect is noted. A listing's exact URL is often unknown after a directory submission: add it via
"I submitted this by hand" or "Monitor a page".

## Outreach

Drafts (subject, opening, relevance, proposal, URL, closing) are generated from your profile and facts collected
(page title, dead links found). They're stored locally; **the app never sends email**. Copy and send them yourself.

## Safety

* Listens on 127.0.0.1 only; Host-header check (DNS-rebinding), custom-header + Origin check on all writes (cross-site requests blocked), strict CSP.
* SSRF guard on every fetch and redirect hop: blocks localhost, private/link-local/reserved ranges, cloud-metadata IPs/hosts,
  credentials in URLs and unusual ports (allowed: 80, 443, 8080, 8443).
  *Residual risk:* DNS is checked just before connecting, so a hostile DNS server could in theory change its answer (DNS rebinding).
* SQL uses parameters only; all UI text is inserted as text (no innerHTML); CSV export neutralises formula injection.
* No shell execution from the web UI. No passwords or API keys are stored (tokens come from environment variables).
* Never bypasses CAPTCHAs, logins, anti-bot systems or rate limits.

## Honest limits

No guarantee of any number of backlinks, link quality, rankings, indexing or speed of results.
No per-site integrations ship with the app (only generic form handling). Many good opportunities
(login, CAPTCHA, editorial review, outreach) will remain manual. JavaScript-only forms cannot be submitted.

## Background jobs

Discovery, assessment, submission and verification run in background threads with progress, pause/resume/cancel,
checkpoints in SQLite, rate limiting, timeouts and logged errors. One active job per website and type.
After a restart, discovery/assessment/verification resume automatically (toggle in Settings); submission jobs are
never auto-resumed. Failed scans never delete existing records.

## Files

```
app.py  main.py  config.json  requirements.txt
modules/  settings db safety crawler quality finder submitter checker outreach reporter exporter jobs
templates/  base index opportunities backlinks settings history
static/css/style.css  static/js/app.js  static/icons/icon.svg
data/sources.json  data/submissions.json   database/backlinks.db (created on first run)
reports/ (export copies)  logs/app.log  tests/test_core.py
```

## What was tested

Unit/integration tests (`tests/test_core.py`, 13 tests, all passing) use a local mock web server: URL validation and SSRF
blocking, duplicate prevention, TXT/CSV import, robots.txt blocking, quality assessment, form mapping with honeypots, the
approval gate, CAPTCHA refusal, no double submission, backlink states (live/nofollow/ugc/not found/redirect/retry after 503),
disappearance detection, outreach, CSV/JSON export, route security, plus job pause/resume/cancel/recovery. The server was started and
checked on 127.0.0.1:8080. Not tested: live internet sources, real third-party directories, and the UI in a real mobile browser
(JavaScript syntax-checked only).
