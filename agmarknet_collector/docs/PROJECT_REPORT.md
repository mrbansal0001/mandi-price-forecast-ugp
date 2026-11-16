# Mandi Price Forecast: System and Model Report

A farmer-facing website that shows **tomorrow's expected mandi price** (₹ per quintal) with a 95% range, the price history and a CSV download. Prices are collected automatically from the Government of India's Agmarknet portal. The site currently covers **Uttar Pradesh** and **Rajasthan**, in English and Hindi.

Figures in this report were measured on 27 October 2026 unless stated otherwise.

---

## 1. Problem and scope

Farmers sell at local mandis (wholesale markets), where the price changes daily. Agmarknet publishes the reported prices, but only as a report that must be filtered by hand (state, commodity, market, dates) and solved with a CAPTCHA each time. It offers no forecast.

The project:

1. collects the reported prices automatically and keeps them up to date;
2. forecasts the next day's modal price for each crop in each market, with an uncertainty range;
3. presents the result in a simple page whose filters follow the official site (Commodity Group, Commodity, Variety, Grade, District, Market).

The scope was narrowed on purpose: first **Uttar Pradesh**, then **Rajasthan**, so each step could be verified against real data before scaling.

## 2. Data

### 2.1 Source
Agmarknet's "Daily Price and Arrival Report" (`agmarknet.gov.in`). Each record is one market, commodity, variety and grade on one date, with minimum, maximum and **modal** price (the most common price of the day) in ₹ per quintal. The model forecasts the modal price.

### 2.2 What was measured about the source
These facts shaped the design:

| Finding | Consequence |
|---|---|
| A report for **all districts of a state** and one commodity can be requested in one call. | One request per commodity per state, not one per market. |
| A whole year (up to 365 days) fits in one request; pages of 5,000 rows are served. | A full-year history is about one request per commodity plus its pages. |
| Every report needs its own CAPTCHA; the access ticket is bound to the exact filters. | One CAPTCHA per commodity is unavoidable. |
| The source throttles: about 100 requests in a burst, then only about 1–2.5 per minute ("too many requests"). | Requests are the scarce resource; the collector paces itself and backs off. |
| States upload with a lag. Uttar Pradesh's newest reports are about **5–6 days** behind; Rajasthan's are about 1 day behind. | Forecasts must handle old last reports honestly (section 5.6). |
| Any year from 2021 can be requested (one year per request). | Older years are available but not collected yet (section 9). |

### 2.3 What is stored

| | Uttar Pradesh | Rajasthan |
|---|---|---|
| Price rows (28 Oct 2025 onward) | about 488,000 | about 197,000 |
| Commodities | 97 | 90 |
| Markets / districts | 251 / 73 | 160 / 37 |
| Series (market + commodity + variety + grade) reporting in the last 30 days | about 4,800 | about 1,700 |

A small Maharashtra pilot (8,700 rows) is kept in the database but hidden.

## 3. System architecture

```
 Agmarknet  ──►  Source client  ──►  SQLite  ──►  Forecast models
 (CAPTCHA,        (OCR, ticket,       (validated     (6 models, per-series
  throttling)      retries, pace)      rows, jobs)     selection, 95% range)
                                          │                  │
                                          └────► FastAPI ◄───┘
                                                   │
                                  Farmer page · Admin page · CSV
```

### 3.1 Components

| Component | Role | File |
|---|---|---|
| **Source client** | Calls the API, reuses the ticket across pages, retries, and classifies throttling and outages as "wait and retry" rather than failures. | `agmarknet/client.py` |
| **CAPTCHA solver** | A local glyph-template OCR (Pillow and NumPy) for the portal's plain six-character images. No remote solver. At most three fresh challenges per report. | `agmarknet/ocr.py` |
| **Storage** | SQLite in WAL mode. Validates rows, sends bad rows to a quarantine table, logs price revisions, tracks each series' revision number. | `agmarknet/storage.py` |
| **Job service** | Runs the history build, daily update, weekly census and forecast rebuild. Jobs are persisted, resumable, and run one at a time (single worker plus an OS file lock). | `agmarknet/service.py` |
| **Forecasting** | Six candidate models, per-series selection and the 95% range. | `agmarknet/forecast.py` |
| **API and scheduler** | FastAPI serves the data and pages; APScheduler runs the schedule. | `agmarknet/app.py` |
| **Web pages** | Plain JavaScript (no build step): the farmer page with an SVG chart, English/Hindi text, and an admin page. | `web/` |

### 3.2 Jobs and schedule (IST)

| Job | When | What it does |
|---|---|---|
| History build | Once per state | One whole-year report per commodity (biggest crops first). Takes hours because it waits out throttling. |
| **Daily update** | 07:00 | Re-requests the last 14 days for the commodities the state has reported in the last 60 days. |
| Weekly census | Sunday 03:00 | Last 30 days for the active commodities plus the 40 least-recently-checked others, to discover new commodities. |
| Forecast rebuild | 00:05 | Local only; makes "tomorrow" correct after midnight. |

One update a day is enough because states upload with a lag of days: a full 14-day recheck of Uttar Pradesh changed only 0.2% of series. The admin page records, after each update, how many rows were added for each data date, so the run time can be tuned from evidence.

### 3.3 Reliability design
- **Resumable work.** Each completed commodity window is recorded per run; after a restart, only unfinished windows are fetched.
- **Deferral, not failure.** A CAPTCHA failure defers only that commodity. Throttling stops the sweep for a cooldown (15, then 30 minutes) and slows the request pace; clean sweeps speed it up slightly. A network outage waits and retries (up to 12 times) without changing the pace.
- **Pausing.** Access denial or a changed response format pauses collection and shows on the admin page; any admin action resumes it.
- **No scraping on page views.** Opening a page only reads stored data.
- **Responsiveness.** The farmer lists are cached and served from the previous result while they rebuild, so a visitor never waits behind the collector.
- **Admin page** is reachable only from the computer running the server.

### 3.4 API

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | Collection state per state, last update, running job. |
| `GET /api/selections?state=` | The filter options for one state, with a flag for series that have a forecast. |
| `GET /api/series/{id}/history?days=` | Reported prices for one series. |
| `GET /api/series/{id}/forecast` | Tomorrow's forecast, range, model, back-test results. |
| `GET /api/series/{id}/history.csv` | Full stored history as CSV. |
| `GET /api/admin/status`, `POST /api/admin/enqueue` | Admin only. |

### 3.5 Testing
80 automated tests cover the client (tickets, throttling), storage (validation, quarantine), the job service (resume, deferral, backoff, pacing, rollover), the forecasting rules (causality, staleness, calibration), multi-state behaviour, the cache and the API.

---

## 4. Forecasting: problem set-up

**Target.** The modal price in a market for one commodity, variety and grade (one *series*) on the next calendar day.

**Input.** The reported prices of that series. Days with no report are kept as **gaps**: they are never filled in or interpolated, and never treated as observations.

**Horizon.** Normally one day. When the latest report is `a` days old, the forecast for tomorrow is `a + 1` days ahead of the last report. Uttar Pradesh's typical last report is 6 days old, so its "tomorrow" forecast is usually a 7-day-ahead forecast. The page says so.

**Training window.** The latest 180 days (a 365-day window was tested and was worse; section 7.3).

**Why simple models.** Mandi prices are *sticky*: the same price can be reported for several days and then jump. Series are short, gappy and irregular. A flexible model fitted to ~100 noisy points per series overfits, and thousands of series cannot be tuned by hand. So the system uses several simple, robust forecasters and selects among them per series using held-out results.

## 5. The forecasting models

All six models use only reported prices and only data before the day being predicted. Let `y_t` be the reported price on day `t`, and `h` the horizon in days.

### 5.1 Last price (persistence / naïve)
`ŷ = y_last`. Tomorrow equals the latest reported price. It is the standard baseline for sticky series, hard to beat when prices rarely move, and it reacts instantly to a jump (including a one-off outlier).

### 5.2 Median of the last 5 reported prices
`ŷ = median(y_{n-4}, …, y_n)` over the last five *reported* prices. The median ignores a single wrong or unusual price, so it follows the typical price but not spikes. It is the most often selected model and the default when a series has too little history to choose.

### 5.3 7-day mean
`ŷ = mean of the prices reported in the last 7 calendar days` (gaps are skipped; if there is none, the last price is used). It smooths day-to-day noise, but lags real trends.

### 5.4 Exponential smoothing (α = 0.3 and α = 0.6)
A weighted average that gives recent prices more weight: `ℓ_t = α·y_t + (1 − α)·ℓ_{t−1}`, starting from the first price, updated only on reported days, with a flat forecast `ŷ = ℓ_t`. α = 0.3 is smoother and slower, α = 0.6 reacts faster. Both are included because markets differ in how quickly they move.

### 5.5 Adaptive Kalman filter (local linear trend)
A state-space model with two hidden states, **level** and **trend**:

- Transition: `level_t = level_{t−1} + trend_{t−1}`, trend persists.
- Update: when a price is reported, the innovation `y_t − level` is split between level and trend by the Kalman gains `K₀ = P₀₀/(P₀₀ + r)` and `K₁ = P₁₀/(P₀₀ + r)`.
- **Gaps:** on a day with no report the filter only predicts, so its covariance `P` grows and confidence falls; it is never fed a made-up value.
- **Adaptive noise.** The observation noise `r` and process noise `q` are re-estimated online as exponentially weighted averages of the squared innovations (with upper and lower bounds), so the filter loosens when prices become volatile and tightens when they are calm. The initial scale is set from the first price (about 3% of its level).
- Forecast: `ŷ = level + h·trend`, floored at 0.

The Kalman filter can capture trends, but on real mandi series it was the **worst single model**: sticky prices followed by jumps produce misleading trend estimates, and it extrapolates them.

### 5.6 Choosing a model per series (rolling-origin back-test)
For each series:

1. **Origins.** A day is a valid forecast origin if at least 60 prices have been seen, at least 20 of the last 30 days were reported, and the price `h` days later is known. The latest 60 such origins are used.
2. **Back-test.** At every origin, all six models forecast the day `h` ahead using only data up to the origin. Their absolute errors are recorded.
3. **Selection.** The earlier origins (all but the last 30) are used to select the model with the **lowest mean absolute error (MAE)**.
4. **Honest scoring.** The model's accuracy is then reported on the **last 30 origins**, which it did not influence.
5. **Fallback.** With fewer than 20 origins, no selection is made and "Median of last 5" is used.

Because `h = age + 1`, a series whose last report is old is back-tested at that same horizon, so its error reflects the horizon actually being forecast.

### 5.7 The 95% range
- Take the chosen model's absolute errors at the same horizon.
- With at least 20 errors: the empirical 95th percentile (interpolated); with 8–19: `1.96 × RMSE`; with fewer than 8: `1.96 ×` the typical day-to-day price move, scaled by `√h`.
- Multiply by **1.3** and never go below **5% of the price level × √h**. Two things make the raw range too narrow: choosing the best model on past errors makes errors look smaller than they will be, and sticky prices give some series near-zero past errors.
- The width is calibrated on the selection origins only and its coverage is checked on the 30 held-out origins.
- Lower bound is clipped at zero.

### 5.8 When a forecast is shown
A forecast is produced only if the series has **at least 60 reported days in the 180-day window, at least 15 of them in the last 30**, and its **latest report is at most 7 days old**. Otherwise the page shows the history and a reason: *delayed* (report older than 7 days) or *insufficient history*. Forecasts are cached per series, date, data revision and model version.

---

## 6. Results

### 6.1 Accuracy
Each model's error was measured at the 30 held-out origins per series (UP: 14,240 tests across 655 series; Rajasthan: 6,715 tests across 427 series). Errors are in ₹ per quintal; MAPE is the mean absolute percentage error.

**Uttar Pradesh**

| Model | MAE | RMSE | MAPE |
|---|---:|---:|---:|
| Adaptive Kalman | 182.1 | 912.8 | 5.93% |
| Last price | 155.1 | 980.7 | 5.00% |
| Median of last 5 | **135.6** | 818.7 | 4.62% |
| 7-day mean | 143.8 | **786.0** | 4.86% |
| Exponential smoothing (0.3) | 140.5 | 786.5 | 4.72% |
| Exponential smoothing (0.6) | 142.7 | 854.4 | 4.69% |
| **Per-series choice** | 137.3 | 819.5 | 4.62% |

**Rajasthan**

| Model | MAE | RMSE | MAPE |
|---|---:|---:|---:|
| Adaptive Kalman | 227.3 | 484.4 | 6.52% |
| Last price | 218.5 | 529.8 | 6.28% |
| Median of last 5 | 217.5 | 474.1 | 6.32% |
| 7-day mean | 224.1 | 465.5 | 6.54% |
| Exponential smoothing (0.3) | 220.3 | **459.0** | 6.39% |
| Exponential smoothing (0.6) | **209.0** | 467.0 | **6.08%** |
| **Per-series choice** | 217.8 | 478.6 | 6.21% |

**Models chosen** (series, UP / Rajasthan): Median of last 5: 307 / 258; Last price: 116 / 58; 7-day mean: 73 / 23; Exponential smoothing 0.3: 54 / 34; Exponential smoothing 0.6: 52 / 31; Kalman: 53 / 23.

### 6.2 What the results say (stated plainly)
- **The Kalman filter is the weakest model** on both states: about 34% worse than the simple median in UP (MAE 182.1 vs 135.6), and about 9% worse than the best model in Rajasthan (227.3 vs 209.0).
- **Simple robust models win.** Median of last 5 and the smoothers are the best fixed choices.
- **Per-series selection did not beat the best fixed model.** In UP it is within about 1% of "Median of last 5" (137.3 vs 135.6), and in Rajasthan it is about equal to it and worse than exponential smoothing 0.6. Its value is **robustness**: it avoids committing every series to a model that suits some series badly, such as the Kalman filter, which only about 5–8% of series still pick. An earlier measurement on a smaller early sample showed a bigger gain, and the full data did not confirm it.
- **Honest conclusion for the report:** for sticky mandi prices over short horizons, a median of recent prices is a strong, hard-to-beat forecaster. Complex models do not pay off, and the extra selection step mainly protects against the worst choice.

### 6.3 Interval coverage
The 95% range contained the actual price in **97.0%** of 13,317 held-out tests (UP) and **94.9%** of 5,203 (Rajasthan). So the range is slightly cautious in UP and on target in Rajasthan.

### 6.4 Coverage of the site

| | Series reporting in last 30 days | With a forecast |
|---|---:|---:|
| Uttar Pradesh | about 4,800 | 655 |
| Rajasthan | about 1,700 | 427 |

Most series cannot be forecast because the report is too old (UP: about 2,900 delayed) or too sparse (UP: about 1,350 insufficient). This is a property of the source's reporting, not of the models.

In UP, of the 655 forecast series, **445 are based on a report 6 days old**, so their "tomorrow" forecast is a 7-day-ahead forecast. In Rajasthan most are based on a report 0–1 days old.

## 7. Experiments that shaped the settings

### 7.1 Freshness rule
Requiring a report within 2 days left 97% of UP series without a forecast. Allowing up to 7 days, with horizon-matched back-tests and a wider range, raised coverage while keeping the held-out coverage near 95%.

### 7.2 History requirement
Lowering "15 or more reported days in the last 30" from 20 to 15 added about 250 series with unchanged accuracy (MAE 155.2 for the chosen model against 167.1 for "last price", coverage 96.7% over 3,681 tests). Series with 10–14 days had too few tests (1,019) and very wide ranges. Series with fewer than 60 reported days cannot be back-tested at all, so they get no forecast.

### 7.3 Window length
Using 365 days of history instead of 180 added 65 forecastable series but made the average error **about 10% worse** (MAE 151.4 vs 137.9 on the same 654 series). Old months pull the short-memory models towards old price levels, so the model uses 180 days.

### 7.4 Interval calibration
A normal-theory interval undercovered (87–91%) because of selection bias and sticky prices. The empirical-quantile range with ×1.3 inflation and a 5% floor reached about 95–97%.

---

## 8. Limitations
- **Sparse, late data.** Only about 10–25% of series qualify for a forecast, and Uttar Pradesh's forecasts are mostly week-ahead. This depends on how states upload.
- **Modal price only.** Arrival quantities, weather, festivals, policy and transport are not used.
- **Short history.** One year is collected, so no seasonal effects are modelled.
- **Single-machine deployment.** Currently the server runs on one laptop. It stops if the machine sleeps or the session is closed.
- **Source dependence.** The method depends on the portal's response format and rate limits, which can change.
- **Two states.** All 36 states and UTs are not yet covered.

## 9. Future work

| Item | Notes |
|---|---|
| **Seasonal view** | The source serves years from 2021. Collect earlier years, then show a year-on-year chart, typical-month price index and a weeks-to-months outlook. Seasonality will not change tomorrow's number but is useful for longer-term planning. Needs about 2 days. |
| **Always-on hosting** | A small cloud server with HTTPS, a password-protected admin page and database backups; also tests whether the source accepts a cloud address. About 1–2 days. |
| **All states** | One request per commodity for all states together would cut requests from about 6,000 to 1,500–2,000 for the history and to about 400 per daily update; about 4–5 days in total. |
| **Local service** | Run the server as a macOS login service so it survives closing the editor. |
| **Arrival-time tuning** | Use the arrival log to choose the best daily update time, or add a second run for fast-reporting states. |

## 10. Reproducing and running

```
cd agmarknet_collector
python -m agmarknet serve --output output        # site at http://127.0.0.1:8000, admin at /admin
python -m unittest discover -s tests              # 80 tests
```

- Farmer page: `http://localhost:8000/` (`?state=Rajasthan`, `?lang=hi`).
- Data: `output/agmarknet.sqlite`.
- Constants for the forecasting rules (`WINDOW_DAYS`, `MIN_ROWS`, `MIN_RECENT`, `MAX_AGE_DAYS`, `WIDTH_INFLATION`, `WIDTH_FLOOR`, `VERSION`) are at the top of `agmarknet/forecast.py`. `VERSION` must be changed whenever the forecasting logic changes, so cached forecasts are rebuilt.

## 11. Repository layout

```
agmarknet_collector/
  agmarknet/   client.py ocr.py storage.py service.py forecast.py app.py runner.py __main__.py
  web/         index.html app.js i18n.js style.css admin.html
  tests/       test_collector.py test_service.py test_app.py
  tools/       diagnose_access.py
  docs/        PROJECT_REPORT.md   (this file)
  output/      agmarknet.sqlite, catalogue.json, logs   (not source code)
```
