# Mandi price forecast (UGP)

Farmer-friendly site showing tomorrow's predicted mandi price (Uttar Pradesh and Rajasthan), built from Agmarknet data.

| Folder | What it is |
| --- | --- |
| `agmarknet_collector/` | Python collector + SQLite, FastAPI app, forecast models, web pages. This is the website. |
| `harvest/` | Earlier Next.js skeleton (unused). |
| `ugp_report/` | UGP report (LaTeX), presentations and figures. |
| `experiment.py`, `fig*.png`, `my_report.tex` | Numerical-experiments report and figures. |

## Run the website

```sh
cd agmarknet_collector
python3 -m venv .venv && .venv/bin/pip install -r requirements-app.txt
.venv/bin/python -m agmarknet serve        # http://127.0.0.1:8000  (/admin is local-only)
```

Requires Python 3.11+. Run the tests with `.venv/bin/python -m unittest discover -s tests` (80 tests, about 1 minute).
Starting the server also starts the scheduled Agmarknet updates (slow, rate-limited, network needed). To only browse the restored data, pause collection from `/admin` first.

## Restore the data

The snapshot was taken on 2026-11-16 and holds prices up to the 2026-11-06 update (Uttar Pradesh, Rajasthan and a small hidden Maharashtra set). The collected data is too large for git, so it is attached to the GitHub **Release** `data-snapshot-2026-11-16` of this repo:

| Asset | Contents |
| --- | --- |
| `agmarknet.sqlite.gz` | Main database (707,420 observations, forecasts) |
| `agmarknet.pre-app-backup.sqlite.gz` | Older backup, predates the Uttar Pradesh work |
| `output-raw.tar.gz` | Raw archived report pages (`output/raw/`) |
| `output-misc.tar.gz` | CSV exports, logs, catalogue, plan (rest of `output/`) |
| `SHA256SUMS` | Checksums of the assets |

```sh
cd agmarknet_collector && mkdir -p output && cd output
gh release download data-snapshot-2026-11-16 --repo mrbansal0001/mandi-price-forecast-ugp
gunzip agmarknet.sqlite.gz agmarknet.pre-app-backup.sqlite.gz
tar -xzf output-raw.tar.gz && tar -xzf output-misc.tar.gz
```

Verified: a fresh clone + the commands above gives a working site and passing tests.

Not stored (regenerable): `.venv`, `harvest/node_modules`, `.cache` (Swift module cache).
