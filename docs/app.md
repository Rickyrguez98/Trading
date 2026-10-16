# Trading app

A local web app over the whole US-equities workflow — select, allocate, choose
a broker option, monitor — so the project can be used without the command
line. It runs on your machine at `http://127.0.0.1:8765`, has no login, and is
only reachable from your own computer. It never places orders.

It is branded **Finance Bull** (logo in `src/trading_app/web/static/brand/`) and
its dashboard charts a year of prices for the stocks you follow, with trend,
volatility, momentum and Fibonacci studies — see
[Market charts](#market-charts-on-the-dashboard). Every page works end to end;
see [Roadmap](#roadmap) for what comes next.

## Install and run

The app needs **Python 3.10 or newer** (current FastAPI, uvicorn and
python-multipart no longer support 3.9). The pipelines themselves still run on
3.9, so the cleanest setup is one Python 3.11 environment for everything:

```bash
cd ~/Desktop/Trading
python3.11 -m venv .venv311
source .venv311/bin/activate
pip install --upgrade pip
pip install -e ".[dev,pdf,app]"          # add ,forecast for the Chronos-2 model
trading-app                               # opens http://127.0.0.1:8765
```

`trading-app --port 9000` changes the port; `--no-browser` skips opening a tab.
Run it from the project folder (or pass `--root`), because it reads the same
configs and output files as the CLIs. Keys in `.env` (`FINNHUB_API_KEY`,
`IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID`) are loaded at start-up and reach the
jobs through the environment; the app never asks for them.

## Pages

| Page | What you do there |
|---|---|
| **Dashboard** | Four headline tiles (ranking size and run status, plans, holdings and broker readiness, latest monitor actions), the **market charts** for your watchlist — one chart per stock or grouped by sector — and recent jobs. |
| **1 · Select** | Start the full-universe selection (replaces the ranking every step reads) or a quick sample run (kept in its own job folder — sample scores are relative to the sample and must never replace the full ranking). Top 25 table. The full run handles Yahoo's rate limits itself (see below). |
| **2 · Allocate** | Enter an amount and a strategy; see each plan's positions in percent and money. |
| **3 · Broker** | Pick option A, B or C (saved in `data/app/settings.json`), see what each needs, create your holdings CSV from the template, or replace it with an upload (the old file is backed up). |
| **4 · Monitor** | Run the monitor with the chosen option (upload a statement PDF for A), an optional target plan and new cash; read the actions, the evidence for each and the outlook. |
| **Job** | Live log of a running job; refreshes every 3 seconds; links to the result. A selection job shows a progress bar per stage (stocks done, time elapsed and left) and highlights rate-limit pauses in the log. |
| **API** | Interactive documentation of the JSON API at `/api/docs`. |

## JSON API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Everything the dashboard shows |
| GET | `/api/ranking?top=25` | Ranking info and the top rows |
| GET | `/api/plans/{strategy}` | One allocation plan's rows |
| GET / PUT | `/api/broker` | Broker options and readiness / select `{"option": "A"\|"B"\|"C"}` |
| GET | `/api/monitor/latest` | The newest monitor result |
| GET | `/api/jobs`, `/api/jobs/{id}` | Jobs, with the log tail |
| POST | `/api/jobs/selection` | `{"mode": "full"}` or `{"mode": "sample", "limit": 50}` |
| POST | `/api/jobs/allocation` | `{"amount": 25000, "strategy": "balanced", "offline": false}` |
| POST | `/api/jobs/monitor` | `{"option": "B", "target_strategy": "balanced", "cash": 5000, "forecast": "bootstrap"}` |
| GET | `/api/market/watchlist` | The charted symbols, and whether you customised them |
| POST | `/api/market/watchlist` | `{"add": "MSFT"}`, `{"remove": "MSFT"}` or `{"reset": true}` |
| GET | `/api/market/chart/{symbol}` | A year of daily candles, volume, SMA 50/200, Bollinger bands, RSI, Fibonacci levels and statistics |
| GET | `/api/market/sectors` | The watchlist grouped by sector: rebased lines, equal-weight index, S&P 500, statistics |

Starting a job returns `202` with the job; a second job of the same kind while
one runs returns `409`; invalid input returns `400`; a chart Yahoo cannot
supply returns `502` with the reason.

## Market charts on the dashboard

**What is charted.** Your *watchlist*: by default the stocks in your holdings
file followed by the best-ranked stocks eligible for allocation (eight in all).
Add any Yahoo symbol — `AAPL`, a share class `BRK.B`, a Mexican listing
`WALMEX.MX` — or remove one with its ×; **Reset** goes back to the default. A
symbol is only added after Yahoo returns prices for it. The list is saved in
`data/app/settings.json` (up to 24 symbols).

**By stock** — one card per symbol: a year of daily candles (split- and
dividend-adjusted) with toggles for

| Study | What it shows | How it is computed |
|---|---|---|
| SMA 50 / 200 | Trend; 50 above 200 is the "golden cross", below is the "death cross" | Simple moving averages of the close; about 23 months are fetched so the 200-day line covers the whole year |
| Bollinger bands | Price against its recent volatility | 20-day average ± 2 standard deviations |
| Fibonacci retracement | Support/resistance levels within the year's swing | 0, 23.6, 38.2, 50, 61.8, 78.6, 100% between the year's low and high; measured down from the high after an advance, up from the low after a decline |
| Volume | Participation | Daily volume, green on up days, red on down days |
| RSI (14) | Momentum; above 70 / below 30 is stretched | Wilder's relative strength index, in its own pane |

and a statistics strip: 52-week range (with where today's price sits), distance
from the high, annualised volatility, maximum drawdown, RSI reading, trend
(price and SMA 50 vs SMA 200), distance from the 200-day average, the current
Fibonacci zone and the 14-day average true range. Moving the cursor over a
chart shows that day's open, high, low and close.

**By sector** — the watchlist grouped by sector (from the ranking, otherwise
from Yahoo): every stock rebased to 0% a year ago, an equal-weight sector
index with its 50-day average, and the **S&P 500** on the same footing, plus
the sector's return, its lead or lag against the S&P 500 in percentage points,
volatility, drawdown and a member table.

**What the studies are not.** They describe past prices. They are not
forecasts and not recommendations; the page says so under the charts. The
monitor (step 4) is where forward-looking statements are made, and only with
their measured accuracy.

**Data.** Yahoo Finance via `yfinance` (unofficial; may be delayed), cached in
`data/app/cache/market/` for six hours (sector names for 30 days), fetched one
symbol at a time 0.4 s apart. A chart request never waits long: if Yahoo
rate-limits the page, it pauses a few seconds and then reports the problem,
instead of the pipelines' 10-minute waits. Charts are drawn with
[TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)
5.2.1 (Apache-2.0), vendored in `static/vendor/` with its licence so the app
works offline and loads nothing from a CDN; its attribution logo stays on the
charts as the licence asks.

## The full selection and Yahoo's rate limits

**Run full selection** runs the same command as the CLI, so it gets the same
protection: calls are spaced 0.4 s apart; a "429 Too Many Requests" answer
pauses the run (30 s, 60 s, … up to 5 minutes, then 10-minute waits) and it
continues with the same stock, so every stock is still priced; a news feed
that keeps refusing is skipped after two tries. Only if Yahoo refuses for
about an hour without a single success does the run stop waiting and report
the remaining stocks as rate-limited. Expect roughly 35–40 minutes. Details:
[data-sources.md](us_equities/data-sources.md#how-429-too-many-requests-is-handled).

## Architecture

```
src/trading_app/
├── services/            # plain Python, no web framework — the reusable core
│   ├── paths.py         #   every file the pipelines read and write
│   ├── commands.py      #   validated argument lists for the three pipeline CLIs
│   ├── jobs.py          #   background jobs: subprocess, log, state on disk
│   ├── status.py        #   readers: ranking, plans, broker readiness, monitor results
│   ├── settings.py      #   app preferences (data/app/settings.json)
│   ├── market.py        #   prices, indicators, Fibonacci, sector index (dashboard charts)
│   └── __init__.py      #   Workflow: one method per user action
├── runner.py            # wraps each job command and records its exit code
├── web/                 # FastAPI: JSON API + server-rendered pages (Jinja2)
│   ├── app.py
│   ├── templates/       #   dashboard, selection, allocation, broker, monitor, job
│   └── static/
│       ├── style.css    #   navy / ivory / gold theme, light and dark, responsive
│       ├── app.js       #   draws the dashboard charts from /api/market/*
│       ├── brand/       #   Finance Bull logo and icons
│       └── vendor/      #   Lightweight Charts 5.2.1 + licence
└── cli.py               # trading-app
```

Design choices:

- **The app runs the same commands you type.** A job is
  `python -m asset_selection.pipelines.<pipeline> …` with validated arguments,
  so the app and the CLIs can never disagree, and every test of the pipelines
  also covers what the app runs.
- **Long work never blocks the page.** Jobs are subprocesses with their state in
  `data/app/jobs/<id>/` (`job.json`, `log.txt`, `exit_code`), so they survive an
  app restart and only one job of each kind runs at a time.
- **The service layer has no web dependency.** A different front end — a
  Streamlit view, a desktop wrapper, a phone app talking to the JSON API — reuses
  `Workflow` unchanged. It runs (and is tested) on Python 3.9 too.

## Security model

It is a single-user tool on your laptop, and it is built so that being open in a
browser does not make it attackable:

- `trading-app` only listens on a loopback address and refuses anything else.
- Requests must carry a localhost `Host` header — this blocks DNS-rebinding
  attacks from websites you visit.
- Form posts and API writes from another origin are refused (`403`), so a
  website cannot start jobs or change settings through your browser.
- Commands are argument lists built from validated input — never shell strings.
- Uploads are limited to `.csv` and real `.pdf` files up to 10 MB, saved under
  `data/app/uploads/` with generated names.
- The app never accepts, stores or displays broker credentials, and never
  places orders. Output is HTML-escaped.

## Data the app writes

All under `data/`, which git ignores: `data/app/settings.json` (broker option,
watchlist), `data/app/jobs/`, `data/app/uploads/`, `data/app/cache/market/`
(chart prices), and the pipelines' usual outputs.

## Tests

```bash
pytest tests/app        # services on any Python; web tests need the app extra (3.10+)
```

Web tests start jobs through a fake process launcher and chart tests use a fake
price downloader, so no pipeline or network call runs; they assert the exact
command the app would run, the pages, the API, HTML escaping, both local-only
guards, the indicator maths on hand-checkable series, caching, the watchlist,
stage progress parsing, and the pinned hash of the vendored chart library.

## Roadmap

- **More charts**: ranking score distribution, allocation sleeves, P/L history
  per position (each monitor run started from the app is kept in its job
  folder, so history accumulates; CLI runs overwrite the latest summary).
- **Chart extras**: your average cost as a line on each held stock, and a
  printable one-page summary for presentations.
- **Holdings editor** in the browser for option B (today: upload or edit the CSV).
- **Scheduling**: a monthly selection run and a weekly monitor run, with a
  notification when a SELL/TRIM appears.
- **Monitor history**: compare this month's recommendations with last month's.
- **Desktop packaging**: a double-click launcher instead of a terminal command.
- **If it is ever exposed beyond your laptop**: add authentication first; the
  current guards assume a single local user.
