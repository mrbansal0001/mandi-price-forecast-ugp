# Agmarknet one-year price collector

A standalone Python collector for the public [Agmarknet daily-price report](https://agmarknet.gov.in/daily-price-and-arrival-report). Website and forecasting development are separate.

**Target:** 26 October 2025 through 25 October 2026, inclusive. All available states and commodities; all markets, varieties and grades returned by the source. This range crosses 13 calendar-month windows. A catalogue entry does not guarantee reported prices.

## Install

Requires Python **3.11+** on macOS or Linux. No browser, paid CAPTCHA service, cloud OCR, account password, or Tesseract installation is needed.

```sh
cd agmarknet_collector
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m unittest discover -s tests -v
```

Only Requests, Pillow and NumPy are direct dependencies (`requirements.txt`); `requirements-lock.txt` also pins the tested transitive dependencies. The supplied DejaVu Sans font is accompanied by its redistribution licence in `assets/LICENSE_DEJAVU`.

## Run in stages

```sh
# Fetch the current state/commodity/market/variety/grade catalogue.
.venv/bin/python -m agmarknet catalogue

# Ten small real report requests, each with fresh automatic CAPTCHA handling.
.venv/bin/python -m agmarknet feasibility

# All pages of Maharashtra onion prices for September 2026.
.venv/bin/python -m agmarknet pilot

# Build the full year's queue without downloading price reports.
.venv/bin/python -m agmarknet plan

# Collect all states and commodities. Ctrl-C saves a resumable checkpoint.
.venv/bin/python -m agmarknet collect

# Resume pending/interrupted batches. Completed batches are skipped.
.venv/bin/python -m agmarknet resume

# Retry failed/blocked batches after inspecting and resolving the error.
.venv/bin/python -m agmarknet resume --retry-failed

# Inspect live SQLite progress; this works while collection is running.
.venv/bin/python -m agmarknet status

# Regenerate CSVs and coverage files while the collector is stopped.
.venv/bin/python -m agmarknet export
```

Use `--output PATH` on every command to select another database/output directory. The default is `output/`, relative to your current directory. Only one collecting/exporting process may use an output directory at a time; an OS file lock prevents overlapping workers and is released when a process exits.

For a bounded collection:

```sh
.venv/bin/python -m agmarknet collect --states Maharashtra,Karnataka --commodities Onion,Tomato --start 2026-09-01 --end 2026-09-30 --max-batches 10
```

State and commodity selections accept catalogue names or numeric IDs. `collect` and `pilot` process their selected scope; `resume` processes the whole pending queue. `--max-batches 0` means no batch-count limit. Parent batches that are split count toward this limit; their children remain queued. `--refresh-catalogue` reloads the official catalogue.

## What has been verified

The implementation passed **31 offline tests in a clean virtual environment**. See `output/feasibility.json` for the timestamped live ten-request test, including requests, challenges, OCR failures, validation results and elapsed time. The latest gate must contain ten trials and at least nine successes before the collector will run. Editing the OCR or client invalidates the gate and requires a new test.

The initial full pilot downloaded **1,578 valid observations over 16 pages** for Maharashtra onions during September 2026, with **zero rejected rows**. It used one automatically solved challenge, then the source's report ticket. These are real source records, not synthetic samples. This does **not** mean the nationwide dataset is complete.

The retrieved catalogue has 36 states/UTs and 605 commodities, producing **283,140** potential state/commodity/month combinations, many possibly empty. There is also one diagnostic empty-report batch outside those monthly combinations. At a three-second minimum request interval it is a multi-day task, potentially longer with pagination and retries. Keep the machine awake and connected. Source changes or persistent access failures pause collection rather than silently omit data.

## Automatic access and rate limits

The client follows the website's report flow: get a challenge image, recognise its six characters locally, submit the answer and matching challenge key, then reuse the returned report ticket while valid. Tickets and answers remain in memory. No server-side verification is disabled or skipped.

The OCR segments glyphs, normalises contrast and compares them with rendered font templates at several sizes. It preserves letter case and rejects low-confidence or unsegmentable images. This implementation is specific to the currently observed image format; a changed font, overlapping text or new challenge type may require maintenance.

- At most **three fresh challenges per report attempt**.
- One sequential HTTP request at a time, at least three seconds between request starts.
- At most five attempts for network errors, HTTP 429 and server errors, with backoff.
- Respect `Retry-After`; cooldowns longer than 60 seconds stop the run and report the required wait.
- Refresh expired report tickets through the same challenge flow.
- Stop on persistent authentication, schema or network failures.

`feasibility` uses September 1–10, 2026 Maharashtra onion reports and validates five actual rows per trial. The subsequent full pilot tests pagination independently. A successful feasibility run is evidence of current access, not a guarantee of permanent availability.

## Storage, provenance and completeness

Outputs:

| File | Contents |
| --- | --- |
| `agmarknet.sqlite` | Observations, batch states, page metadata, audit events, quarantined rows and price revisions |
| `prices.csv` | Clean observations, original names/units, source URL, requested IDs and retrieval timestamps |
| `raw/<batch>/<attempt>/<page>.json` | Source responses, with short-lived report-access credentials removed |
| `catalogue.json` | Source filter catalogues |
| `feasibility.json` | Live access gate and individual trial results |
| `pilot_prices.csv`, `pilot_validation.json` | Frozen pilot dataset and validation evidence |
| `coverage.json` | Counts, actual date range, queue status and last requested scope |
| `batch_status.csv` | Every planned batch, including pending, split, empty, failed and blocked entries |
| `series_coverage.csv` | Per-series observed dates and gaps between the first and last observed date |
| `quarantine.csv` | Rejected source rows with reasons |
| `quality_flags.csv` | Large price changes retained for review, not deleted |

Exports update approximately every five minutes, at the next completed batch, and when collection stops. `status` reads live committed database counts. CSV exports may lag live SQLite while a batch is active.

Each batch is one state, commodity and date interval, initially a calendar month. Reports above 5,000 rows are split into smaller date windows, then into individual markets when necessary. The collector checks reported totals, current page, total pages and repeated-page fingerprints. It will not mark a truncated report complete. Responses without explicit empty-result evidence are errors; the verified `404 / No data available` response is recorded as empty.

A batch's valid rows and final status commit together in one SQLite transaction. If interrupted, that batch restarts at page one to avoid mixing pagination from different source snapshots; archived partial pages remain available. Completed batches are skipped. Root `split` batches are covered by their child checkpoints.

The source spells modal price **`model_price`** and nests rows under `data.records[0].data`; these details are covered by a recorded-response fixture test. Dates, state/commodity selection, units, finite non-negative numbers and `minimum <= modal <= maximum` are validated. Known kg/tonne units are converted to INR/quintal, retaining the source unit; unknown units are quarantined. Missing grade/variety is labelled `Not reported`. IDs absent from the report remain null rather than being invented.

Uniqueness is date + state + district + market + commodity + variety + grade, with normalised whitespace and case-insensitive matching. Re-importing the same observations does not create duplicates. Corrected prices update the observation and preserve its prior value in the revisions table. Conflicting duplicate rows within one report are quarantined for review.

**No missing-date interpolation, zero filling, or fabricated observations.** Zero prices actually reported by the source remain zero. Gaps mean no collected observation; they do not establish that a market was closed. A `complete_with_quarantine` batch has accounted for all received rows, but has data-quality issues; the overall queue is not labelled fully complete while these remain.

## Updating and extending

Use `collect` with a new date interval for new reports; overlapping observations upsert by their natural key. Preserve this historical year's original coverage report when switching to ongoing collection. Back up SQLite using its backup API or after stopping the collector; do not copy only the main file while its WAL is active.

The HTTP/OCR workflow lives in `agmarknet/client.py` and `agmarknet/ocr.py`. Parsing and SQLite operations live in `agmarknet/storage.py`; batching, checks and orchestration live in `agmarknet/runner.py`. Alternative sources would need separate adapters and field/coverage validation. No third-party source is silently substituted.

## Tests

`python -m unittest discover -s tests -v` runs deterministic offline tests for case-sensitive OCR, ambiguous glyphs, malformed images, exact source response parsing, units, dates, source mismatches, explicit empty responses, rate limits, CAPTCHA limits, report tickets, atomic rollback, idempotency, corrected prices, quarantine, pagination, truncation, missing dates and queue planning. The live feasibility report and full-pilot raw archives provide separate integration evidence.

## Farmer forecast site (Uttar Pradesh)

A separate workflow from the nationwide queue above (which stays inactive). It covers **all of Uttar Pradesh**: every commodity, district and market the source reports. Maharashtra data collected earlier stays in the database but is no longer updated or shown.

```sh
.venv/bin/python -m pip install -r requirements-app.txt
.venv/bin/python -m agmarknet serve          # http://127.0.0.1:8000  (admin: /admin, this computer only; ?lang=hi for Hindi)
```

**Collection strategy (measured against the live source).** The report accepts all-districts queries, 5,000-row pages and a whole year in one request (UP wheat alone is about 90,000 rows), so the history build is one report per commodity (big crops paged), not a state × crop × month queue. `commodity` takes exactly one id, and each report needs its own CAPTCHA because the source ties a ticket to the exact filters, so every commodity costs two requests.

**Requests are the scarce resource.** The source allows a burst of roughly 100 requests and then sustains only about 2.5 per minute ("too many requests" otherwise). Hence:

* The one-time history build covers the 408 commodities in groups likely to have prices (vegetables, cereals, pulses, fruits, spices, oil seeds, oils, dry fruits, fibre crops, beverages, others), biggest crops first, and takes **several hours** because it waits out throttling. Flowers, forest products, drugs, medicinal plants and livestock (0 hits in the first 21 probes) are left to the weekly census.
* Updates and the census therefore ask only about commodities that matter:

| Job | When (IST) | What it requests |
| --- | --- | --- |
| update | 07:00 | last 14 days, for the commodities UP reported in the last 60 days |
| weekly census | Sunday 03:00 | last 30 days for the active commodities plus the 40 least recently checked others (finds new commodities and corrections) |
| forecast rebuild | 00:05 | local only; makes "tomorrow" correct after midnight |

A CAPTCHA failure on one commodity defers just that commodity; the run is retried automatically (up to 12 times) and finished work is never repeated. A "too many requests" reply stops the sweep for a cooldown (15, then 30 minutes) and slows the learned request pace by half; clean sweeps speed it up slightly (the pace is stored in the database). Access denial, a changed response format or exhausted retries pause collection and show in `/admin`; any admin action resumes it. After a restart one catch-up update runs, only if the last one is more than 24 hours old, rather than replaying missed schedules. One update a day is enough because states upload to Agmarknet with a lag of days (UP about 5 days, Rajasthan about 1; a full 14-day re-check of UP found changes in 0.2% of series); the admin page's "Data arrivals" table records the rows each update added per date, so the run time can be tuned from evidence. Opening a page only reads stored data.

**UP reports arrive late.** In practice the newest 4–5 days are mostly missing at any moment (about 700 rows a day for older dates against about 60 for the latest) and fill in afterwards, which is why each update re-requests 14 days.

Raw source pages are archived for the history build; the frequent update runs store validated rows with provenance (batch id, retrieval time, source URL) but not raw pages, to keep disk use bounded.

**Farmer page.** Filters follow the official report's order: Commodity Group → Commodity → Variety → Grade → District → Market (State is fixed; Price/Arrivals and the date range are replaced by "tomorrow" and a 30/90/180-day chart toggle). Variety and Grade appear only when a real choice exists, and the best-reported option is the default. An "Only markets with a forecast" filter (on by default) hides series that cannot be forecast yet (too little history or silent for over a week); turn it off to browse their history. Series silent for more than 30 days are hidden. English and Hindi (`?lang=hi`) UI text.

**Forecasts.** For every series the service back-tests six candidate models (adaptive Kalman, last price, median of last 5, 7-day mean, two exponential smoothers), picks the one with the lowest error on the earlier back-test days, and reports its error on the most recent 30 days it was not chosen on. On real series this per-series choice beat every fixed model (held-out MAE 74 against 89.5 for the Kalman filter and 84.8 for last price). The 95% range is the model's own empirical error quantile at the same horizon, inflated by 1.3 and floored at 5% of the price (`WIDTH_INFLATION`, `WIDTH_FLOOR` in `forecast.py`, calibrated on held-out days because sticky mandi prices otherwise undercover); measured coverage on held-out days is shown on the admin page. A forecast needs at least 60 reported dates (15 in the last 30; series with 15–19 had the same back-test accuracy and ~97% coverage on 3,681 held-out tests, sparser ones had too few tests and very wide ranges). Because UP reports arrive late, a series whose latest report is up to 7 days old is still forecast, with the horizon, interval and a notice reflecting that age; older than that, the page shows history and a "delayed" message. Missing dates stay gaps; back-tests use only earlier data.

Code: `agmarknet/service.py` (jobs, sweeps, forecast cache), `agmarknet/app.py` (API, scheduler), `agmarknet/forecast.py` (models), `web/` (farmer and admin pages, `i18n.js` for English/Hindi text). Tests: `python -m unittest discover -s tests -v` (offline).

## Several states

`STATES` in `agmarknet/service.py` lists the tracked states (Uttar Pradesh, Rajasthan; the first is the default). Jobs, history flags, update times and the weekly census are kept per state (`app_runs.state`, settings such as `history_complete:Rajasthan`); forecasts are shared. The farmer page has a state toggle (`?state=Rajasthan`) and the admin page a per-state table and a state selector for actions.

A state added after Uttar Pradesh does not probe all ~400 commodities: its history starts from the commodities already seen in the other states plus the local specialities in `EXTRAS`, and the weekly census (40 least recently checked commodities) discovers the rest. Short updates run ahead of a long history build, so tomorrow's forecasts for finished states do not wait for it. To add another state, add it to `STATES` (source id from `catalogue.json`) and, if useful, `EXTRAS`; the server then builds its history on the next start.
