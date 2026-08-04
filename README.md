# ⚡ Pulsfi LeadGen Engine — AI-Powered B2B Outreach Automation

A local-first, low-cost pipeline that finds local businesses, audits the ones
with a website, writes AI-personalised outreach for both branches, and
deploys a "Trojan Horse" demo landing page to convert them into clients.

---

## 🏗 Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    DASHBOARD (dashboard/index.html)                   │
│   New Campaign panel → query + state + city + categories → Run       │
└──────────────────────────────┬───────────────────────────────────────┘
                                │ POST /api/campaigns
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                api_server.py — Flask control-plane                    │
│         background job → run_pipeline() → poll for progress          │
└───┬──────────────┬───────────────┬───────────────┬───────────────────┘
    ▼              ▼               ▼               ▼
┌────────────┐  ┌───────────┐  ┌───────────┐  ┌─────────────┐
│  scraper   │  │ enricher  │  │ analyzer  │  │email_generator│
│ Google     │  │ Gemini +  │  │ (website  │  │  Gemini —    │
│ Places     │  │ DuckDuckGo│  │  audit,   │  │  dual-branch │
│ (primary)  │  │           │  │  scored)  │  │  copy        │
│ + OSM      │  │           │  │           │  │              │
│ fallback   │  │           │  │           │  │              │
└────────────┘  └───────────┘  └───────────┘  └─────────────┘
                                                       │
                        ┌──────────────────────────────┴───────┐
                        ▼                                      ▼
              demo-site/ (per-lead,               agency-site/ (Pulsfi's own
              deployed to Vercel,                 services + contact page —
              personalised via URL params,        where demo-site CTAs and
              shows the audit scorecard           "no-website" emails send
              inline for website_upgrade leads)   prospects to book in)
```

### The two branches, end to end

| | **No website** | **Has a website** |
|---|---|---|
| Scraper tags it | `strategy = no_website` | `strategy = website_upgrade` |
| Analyzer | skipped | full scored audit (`analyzer.py`) — SSL, speed, mobile, SEO, favicon, social tags → 0-100 score + letter grade |
| Email | "we built you a free site" pitch | cites up to 2 real audit findings + free redesign pitch |
| Demo link | plain personalised landing page | same page **plus** an inline "Your Free Website Audit" scorecard (decoded client-side from a `?report=` token — no backend call needed) |

---

## 🚀 Quick Start

### 1. Install dependencies
```bash
cd scripts
pip install -r requirements.txt --break-system-packages   # or use a venv
```

### 2. Configure `.env`
Copy `.env.example` → `.env` and fill in:
```
GEMINI_API_KEY=...
GOOGLE_MAPS_API_KEY=...          # Places API — required for fast, accurate discovery
GMAIL_ADDRESS=...
GMAIL_APP_PASSWORD=...
VERCEL_BASE_URL=https://your-demo.vercel.app
SENDER_NAME=Your Agency Name
```

> **Why `GOOGLE_MAPS_API_KEY`?** OpenStreetMap/Overpass frequently returns empty
> results or times out on common niches (hotels, restaurants). Google Places
> returns accurate names, phones, websites and coordinates in under a second.
> Without this key the engine falls back to the slower OSM path.

### 3. Start the control-plane API
```bash
python scripts/api_server.py
# → serving on http://127.0.0.1:5055
```

### 4. Open the dashboard
```
dashboard/index.html
```
Type a search query (optional), pick a **state**, optionally a **city**, and
one or more **categories** (Real Estate, Restaurants & Cafes, …), then hit
**⚡ Run Campaign**. Progress streams live; when it finishes, the leads load
straight into the Triage Queue — no manual CSV export/import step anymore.

### 5. CLI alternative
```bash
python scripts/pipeline.py --state California --category-id real_estate --city Fresno --query "family" --dry-run
```

### 6. Deploy the two static sites
```bash
cd demo-site && npx vercel --prod      # per-lead personalised landing page
cd agency-site && npx vercel --prod    # Pulsfi's own services/contact page
```

---

## 📁 Directory Structure

```
leadgen-engine/
├── scripts/                  Python backend
│   ├── api_server.py         ★ NEW — Flask control-plane for the dashboard
│   ├── pipeline.py           Master orchestrator (scrape → enrich → analyze → generate → send)
│   ├── scraper.py            Overpass scraping, mirror rotation, city/query filters
│   ├── enricher.py           Fills missing contact/website data
│   ├── analyzer.py           ★ REWRITTEN — full scored website audit (was: 1 flaw string)
│   ├── email_generator.py    Dual-branch Gemini email copy, cites real audit findings
│   ├── email_sender.py       SMTP dispatch with DNS pre-flight checks
│   ├── config.py             ★ EXPANDED — friendly CATEGORY_CATALOG, OVERPASS_MIRRORS
│   └── requirements.txt
├── dashboard/                 Internal triage & campaign-launch UI
│   ├── index.html            ★ NEW — "New Campaign" launcher panel
│   ├── js/campaign.js        ★ NEW — talks to api_server.py, polls job progress
│   └── js/app.js             Triage table, CSV import (kept as offline fallback)
├── demo-site/                 Per-lead "Trojan Horse" landing page (Vercel)
│   ├── index.html            ★ UPDATED — added audit-scorecard section, phone CTA
│   ├── js/script.js           ★ FIXED — this is the file actually loaded by index.html;
│   │                            now also renders the audit report + phone link
│   └── (js/dynamic.js removed — it was dead code, never `<script>`-included,
│         and used mismatched ?name/?type params instead of the live ?biz/?cat)
├── agency-site/               ★ NEW — Pulsfi's own services/contact landing page
│   ├── index.html            Find-Fix-Enhance-Assure methodology, live audit-scan
│   │                          hero visual, animated counters, contact form
│   ├── css/style.css
│   └── js/script.js
└── data/                      CSV outputs + inbound_contacts.csv (from agency-site form)
```

---

## 🔧 What changed in this pass (bug fixes & enhancements)

1. **Fixed a real bug**: `scraper.py` generated demo URLs with `?biz=&cat=`
   but the page's actual script expected different names — audit context and
   personalisation were silently breaking. Confirmed which JS file `index.html`
   really loads and aligned everything to it; removed the orphaned duplicate.
2. **Category catalog**: OSM tags are no longer exposed raw. The dashboard now
   shows "Real Estate", "Restaurants & Cafes", etc., each mapped to the right
   OSM `key=value` pairs (`config.CATEGORY_CATALOG`).
3. **Overpass mirror rotation**: one dead/rate-limited mirror no longer kills
   a whole run — `scraper.py` rotates across 4 public mirrors with per-mirror
   retry/back-off.
4. **City + free-text query filtering**: the scraper narrows results
   server-side by category (fast, low-load) and client-side by city / business
   name — this is what powers the dashboard's search box.
5. **Real audit reports**: `analyzer.py` now runs 8 weighted checks (SSL,
   speed, mobile, title, meta description, H1, favicon, social tags) into a
   0–100 score + letter grade, instead of returning a single flaw string.
6. **Personalisation upgrade**: emails now cite up to 2 specific audit
   findings; the demo site decodes a compact, signed-free base64 report token
   from the URL and renders a live scorecard — fully static, no backend call.
7. **Campaign launcher**: the dashboard can now trigger and monitor a real
   pipeline run (`api_server.py`), instead of requiring a CLI run + manual
   CSV import.
8. **New agency-site**: a standalone, animated services/contact page that
   demo-site CTAs and "no-website" outreach emails point to.

---

## 📈 Scaling this beyond a single operator

The pieces above are intentionally sized for "one person running local
campaigns," but each has a clear upgrade path:

- **Job runner**: `api_server.py`'s in-memory `JOBS` dict + background thread
  → swap for Celery/RQ + Redis when you need multiple concurrent operators
  or retries across restarts.
- **Storage**: CSV files in `data/` → Postgres (or Supabase) once you need
  cross-campaign dedup, reporting, or multi-tenant access control.
- **Scraping**: Overpass mirrors → add a paid provider (e.g. Google Places)
  as a fallback tier once volume outgrows what free OSM mirrors can sustain.
- **Analyzer**: current checks are static-HTML-based → add a headless
  browser (Playwright) pass for JS-rendered sites, and/or Google PageSpeed
  Insights API for real Core Web Vitals.
- **Email sending**: single Gmail SMTP account → move to a transactional
  provider (SES/Postmark) with domain warm-up once volume increases, to
  protect deliverability.
- **Multi-tenant**: `api_server.py` routes are already parameterised by job;
  adding an `agency_id` to `CATEGORY_CATALOG`/`JOBS`/CSV paths is the main
  change needed to run this for multiple client agencies from one deployment.

---

## 🐞 Second pass — bugs found and fixed through actual testing

The items above were architecture/feature work. This pass re-ran everything
against real (and synthetic) inputs and found + fixed genuine bugs:

1. **Critical: category filtering silently did nothing.** `pipeline.py`
   defaulted `categories` to *every* OSM tag before `scrape_leads()` ever
   saw `category_ids` — so picking "Real Estate" in the dashboard actually
   scraped all 12 categories anyway. Verified with a mocked run (saw all 29
   tags go out), fixed, re-verified only the selected category's tags are
   sent now.
2. **Lead-dropping bug in `enricher.py`.** It ran a second, fuzzy Gemini
   "is this outdated?" judgment and silently dropped any lead Gemini called
   "modern" — even when `analyzer.py`'s structured audit had already found
   real flaws. Removed; enrichment no longer ever drops a lead.
3. **Emailed demo links were missing the audit report.** `email_generator.py`
   rebuilt the demo URL from scratch instead of reusing the one `scraper.py`
   already built, dropping the `phone` and `report` params — so the actual
   link sent to a prospect wouldn't show their personalised audit scorecard.
   Fixed to reuse the existing URL.
4. **Demo page report decoding could silently fail.** The audit-report
   token used `decodeURIComponent(atob(...))`, which throws (hiding the
   whole report section) if any issue title contains a literal `%`.
   Reproduced, fixed with a proper UTF-8-safe decode, reverified.
5. **Systemic NaN-stringification bug** (found via testing, not inspection):
   `str(row.get(x, "") or "")` looks safe but isn't — after any CSV
   round-trip, an empty cell becomes `float('nan')`, which is *truthy* in
   Python, so this pattern silently produced the literal text `"nan"` in
   business names, cities, phone numbers and demo URLs (e.g. a phone link
   becoming `tel:nan`). Found in 10 places across `scraper.py`,
   `enricher.py`, `email_generator.py` and `email_sender.py`. Added a
   shared `config.safe_str()` helper and fixed every occurrence; verified
   with an actual CSV round-trip reproduction before and after.
6. **Triage showed a fake placeholder subject.** Email subjects were only
   computed at send-time, so the dashboard/API always showed "Quick
   question" regardless of the real subject. `email_generator.py` now
   computes and stores `email_subject` per row, and `email_sender.py`
   prefers that stored (operator-editable) value over recomputing it.
7. **Dead code removed.** `demo-site/js/dynamic.js` was never
   `<script>`-included by `index.html` and used mismatched param names —
   deleted to prevent future confusion.
8. **`duckduckgo_search` → `ddgs` rename** — added a fallback import so
   enrichment doesn't break when the old package is eventually pulled.

All of the above were confirmed with actual test runs (a local HTTP server
for `analyzer.py`, synthetic Overpass payloads for `scraper.py`, a Flask
test client for `api_server.py`, mocked-Gemini runs for `email_generator.py`,
and a real end-to-end `pipeline.run_pipeline()` call that hit live
DuckDuckGo/Gemini and degraded gracefully) — not just re-reading the code.

---

## 🌍 Third pass — fixing real-world Overpass timeouts (from production logs)

Running the dashboard for real surfaced a pattern the earlier passes didn't
catch: every Overpass mirror exhausting its retries and rotating, over and
over, for minutes at a time. Root cause and fix:

1. **The actual bug: city selection wasn't shrinking the search area.**
   `build_overpass_query()` always queried the **entire state** — city
   narrowing only happened client-side, *after* the (huge, slow) state-wide
   fetch. A whole-state query across several category tags is exactly the
   kind of heavy request free public Overpass mirrors reject or time out
   on. Fixed: when a city is given, the query now scopes directly to that
   city's OSM administrative boundary (`area["name"="City"]["boundary"=
   "administrative"]["admin_level"~"8|9|10"]`), so the search area — and
   therefore the load on the mirror — shrinks dramatically. State-wide
   queries (no city given) still work as before, just slower by nature;
   the dashboard now recommends specifying a city and the progress message
   says so explicitly.
2. **Latent crash bug found while fixing #1**: adding a regex call in
   `query_overpass()` exposed that `re` was only ever imported *locally*
   inside `parse_elements()`, not at module level — a genuinely new
   `NameError` would have fired the moment the new timeout-parsing code
   ran. Fixed with a proper module-level import; verified with a direct
   call that would have thrown before the fix.
3. **Adaptive HTTP timeout.** The HTTP-level request timeout was a fixed
   200s regardless of query size — so even a fast, small city-scoped query
   would wait up to 200s per attempt if a mirror hung. It's now derived
   from the query's own internal `[timeout:N]` directive (60s for
   city-scoped, 180s for state-wide) plus a small buffer, so a bad mirror
   is abandoned in proportion to how big the query actually is.
4. **User-Agent changed from a browser-spoofed string to a clean,
   policy-compliant bot identity** (`PulsfiLeadGenBot/1.0 (+url; contact:
   email)`) — Overpass's usage policy explicitly asks for identifiable
   script UAs rather than faked browser strings, and some mirrors' abuse
   detection penalizes the latter.
5. **Faster mirror rotation.** Reduced per-mirror retries from 3 to 2 and
   backoff from 10s to 6s — a genuinely overloaded mirror is now abandoned
   for a fresh one sooner instead of being hammered three times with
   growing waits first.
6. **Clearer failure signal.** A city-scoped query returning 0 raw
   elements (e.g. OSM has no mapped administrative boundary for that exact
   city name) now logs an explicit explanation instead of looking
   identical to a network timeout.

Verified: unit tests confirming city-scoped queries never reference the
state area, the adaptive HTTP timeout is derived correctly, the new
User-Agent is actually sent, and the previously-undetected `NameError`
does not fire — plus a full regression re-run of the analyzer/scraper/
pipeline suite to confirm nothing else broke.

---

## 🔥 Fourth pass — premium demo-site redesign + hot-lead tracking

### Demo-site redesign
Full rebuild of `demo-site/` around a "live diagnostic scan" concept: a
sticky split-screen layout (personalised hook + embedded Calendly widget
on the left, scrollable audit evidence on the right), a themed loading
sequence, an animated radial audit-score gauge, and a category-breakdown
chart derived entirely from the real audit report (not mocked data). The
exact `?biz=&city=&cat=&phone=&report=` URL contract `scraper.py`
generates was left untouched — the email pipeline needs zero changes.
Also removed an orphaned, never-loaded `css/styles.css` (991 lines) left
over from an earlier iteration.

### Hot-lead engagement tracking (`/api/track`)
The single highest-leverage addition for actual conversion: knowing the
moment a prospect opens their page. `demo-site/js/script.js` fires a
fire-and-forget beacon via `navigator.sendBeacon` on page load
(`page_view`), and hooks into Calendly's own postMessage events for real
intent signals — `booking_intent` when a visitor picks a date/time,
`booking_confirmed` when they actually book. `api_server.py`'s new
`/api/track` endpoint logs every event to `data/lead_engagement.csv` and
sends an instant email alert (reusing the same Gmail SMTP credentials
`email_sender.py` already uses) — debounced to one alert per lead per 30
minutes, except `booking_confirmed`, which always alerts immediately
since an actual booking should never be suppressed. Tracking is
opt-in and fails completely silently if unconfigured — set
`window.PULSFI_TRACK_ENDPOINT` at the top of `demo-site/index.html` once
`api_server.py` is deployed somewhere publicly reachable (it currently
binds to `127.0.0.1` for local use).

**A real bug this surfaced**: the first implementation used
`navigator.sendBeacon` with a `Blob` typed `application/json`. That's not
a CORS-safelisted content type, so the browser required a preflight —
and the actual beacon POST silently never followed through after that
preflight in Chromium (confirmed via a real cross-origin browser test,
not just a unit test: the OPTIONS preflight succeeded every time, the
POST never arrived). Fixed by typing the Blob `text/plain` (CORS-safelisted,
no preflight) — Flask's `get_json(force=True)` parses the body as JSON
regardless of the declared content type. This is exactly the kind of bug
that only shows up under real cross-origin conditions, which is why it
was caught by running an actual browser against an actual second server
on a different origin rather than trusting a mocked test.

---

## 🎯 Fifth pass — fixing "no leads scraped, all CSVs empty"

Root cause: my earlier Overpass city-scoping fix (third pass, above) scoped
searches to `area["name"="City"]["boundary"="administrative"]
["admin_level"~"8|9|10"]`. That requires OSM to have a mapped
**administrative boundary polygon** for that exact city name — and huge
numbers of real places (unincorporated communities, suburbs, anywhere
colloquially called a "city" that isn't one administratively) simply
don't have one. The query was correctly finding *zero* elements for those
places — not failing, just legitimately empty — which is why every CSV
downstream was empty with no error to point at.

**Fix**: `scraper.py` now geocodes the city via OSM's Nominatim first
(`geocode_city()`) and searches by radius around those coordinates
(`around:15000,lat,lon` in the Overpass query) — this has no dependency
on any administrative boundary existing at all, and works for essentially
any place Nominatim can locate. The old boundary-name match is kept only
as a fallback for when geocoding itself fails (network issue, truly
unrecognised name).

**Also fixed**: zero-result runs used to just log a warning to
`pipeline.log` with no visibility anywhere else. Now `pipeline.py` builds
a specific, actionable message (data-coverage gap vs. geocoding failure
vs. wrong category) and threads it through the progress callback into
`api_server.py`'s job status, and the dashboard (`campaign.js`) surfaces
it as an error toast instead of a silent empty table — so "why is
everything empty" has a real, visible answer next time instead of
requiring a support conversation to diagnose.

Verified with mocked Nominatim/Overpass responses (can't reach either
live from this environment): geocoding success → radius query
constructed correctly; geocoding failure → falls back to boundary
matching without crashing; a full zero-result run traced end-to-end from
`pipeline.py`'s summary through `api_server.py`'s job status to confirm
the dashboard would actually see the specific reason, not just "done"
with an empty table.

---

## 🔧 Sixth pass — auditing an externally-modified upload ("email discovery" + "send" features)

A different tool/session had added two new features on top of this
project (automated email discovery from a lead's website, and a
dashboard "Send Approved Emails" button) and supplied an analysis report
claiming both worked. Neither did — verified by actually running the
code, not by reading the report's claims.

1. **`enricher.py` had a genuine `SyntaxError`.** The `try` block in
   `fetch_review_sentiment` was missing its `except` clause entirely —
   truncated when `discover_email_from_website` was inserted right after
   it. This means the file could not be imported at all, so **Phase 2
   failed on every single pipeline run, unconditionally**. This alone
   fully explains "leads scraped, demo links generated, but no email
   written" — the pipeline never got past Phase 2 to reach Phase 3
   (email generation).
2. **A second, conditional crash was stacked underneath that**: the new
   email-discovery code referenced `enriched_row["email"] = ...` before
   `enriched_row` was ever created — a real `UnboundLocalError`,
   reproduced in isolation. This would have crashed Phase 2 specifically
   whenever email discovery *succeeded* (i.e. the feature working
   correctly), even after fixing the syntax error above. Fixed by
   creating `enriched_row` first, and using `safe_str()` when checking
   for a website URL so a NaN cell (truthy in Python) can't trigger a
   wasted discovery attempt on a lead with no real website.
3. **`dashboard/js/app.js` had a stray extra `}`** — a JS syntax error
   that breaks parsing of the whole file, not just one function. This
   likely explains reports of the dashboard "not loading data correctly"
   more broadly than just the send button.
4. **The "Send Approved Emails" button was wired to nothing.** It read a
   `jobId` from a URL query parameter that was never actually set
   anywhere (`campaign.js` tracks the job ID only in memory) — guaranteed
   "Job ID not found" on every attempt. Fixed by having `campaign.js`
   store the active job ID on `AppState` when a campaign's leads load.
5. **Even with a valid job ID, sending would have emailed the whole
   campaign, not just approved leads.** The backend `send_emails`
   endpoint received a `leads` array from the frontend but never read
   `request.get_json()` at all — it just called `batch_send` on the full
   campaign CSV. Fixed: the endpoint now reads the approved leads' email
   addresses, filters the campaign CSV down to exactly those rows, writes
   that subset to its own file, and sends only that — verified with a
   test that includes a second, unapproved lead in the CSV and confirms
   it's excluded from what actually gets sent.
6. **A real XSS regression I introduced while fixing a cosmetic
   double-escaping issue** (business names with apostrophes were
   rendering as literal `Joe&#039;s Cafe` text). Removing `escapeHtml()`
   upstream fixed the visible cosmetic bug but broke the one place that
   escaping was load-bearing: the loading sequence builds its text via
   `innerHTML`, not `textContent`. Caught this myself with a follow-up
   XSS test (not assumed safe) — fixed by escaping specifically at that
   `innerHTML` boundary instead of upstream, verified both the clean
   apostrophe display and no-XSS hold simultaneously.
7. **Leaked real credentials, again.** Both `.env.example` files
   (project root and `scripts/`) still contained what look like real
   Gemini/Gmail credentials. Sanitized both. **If these are your real
   credentials, rotate the Gmail App Password and regenerate the Gemini
   API key** — they've now been included in multiple uploaded files.
8. **Two dead legacy JS files removed** (`dashboard/js/emailGen.js`,
   `dashboard/js/scraper.js`) — an earlier, fully-client-side
   architecture (browser calling Overpass/Gemini directly) that predates
   the Python backend and was never referenced by `index.html`.
9. Re-applied the Vercel placeholder-domain fix from the previous pass
   (this upload's base was missing it) — plus the `require_demo_url`
   hard-block before any real send.

Verified with actual end-to-end runs, not inspection: reproduced the
original `UnboundLocalError` in isolation before fixing it, ran a full
mocked Phase 1→2→3 pipeline and confirmed a real `email_body` lands in
the output CSV, and tested the approval-filtering fix with a second
unapproved lead deliberately included in the CSV to confirm it's
excluded from what actually gets sent.

---

## ⚖️ A note on the scraper's approach

The brief described "scrape all active leads, then filter for the selected
category." We deliberately query Overpass **only for the requested category
tags up front** instead — it returns the same final result set, is
dramatically faster, and is far kinder to the free public Overpass mirrors
(no bulk-then-discard requests). City and free-text narrowing still happen
client-side after parsing, since Overpass's OSM `addr:city` tagging is too
inconsistent to filter reliably server-side.
