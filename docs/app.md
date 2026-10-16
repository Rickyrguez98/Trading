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
| **Dashboard** | An upload box for your trades (confirmation PDFs or an orders spreadsheet), four headline tiles (ranking size and run status, plans, holdings, latest monitor actions), the **market charts** for your watchlist — by default your own stocks — one chart per stock or grouped by sector, and recent jobs. |
| **Portfolio** | Import your trade confirmation PDFs (any number at once); see every purchase lot with its sale or its live value, your realized and open results, charts of your stocks with your buys and sells marked, and download it all as CSV. See [Portfolio](#portfolio-from-your-trade-confirmations). |
| **1 · Select** | Start the full-universe selection (replaces the ranking every step reads), a **my stocks only** run (fundamentals and news for just your holdings, a few dozen requests) or a quick sample run (kept in its own job folder — sample scores are relative to the sample and must never replace the full ranking). Top 25 table. The full run handles Yahoo's rate limits itself (see below). |
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
| GET | `/api/market/chart/{symbol}?since=YYYY-MM-DD` | A year of daily candles (or back to `since`), volume, SMA 50/200, Bollinger bands, RSI, Fibonacci levels and statistics |
| GET | `/api/market/sectors?symbols=A,B` | The watchlist (or the given symbols) grouped by sector: rebased lines, equal-weight index, S&P 500, statistics |
| GET | `/api/portfolio` | Summary and one row per purchase lot (`?live=true` refreshes prices first) |
| GET | `/api/portfolio/quotes` | Live prices and results for open lots; Yahoo is asked at most every 5 minutes |
| GET | `/api/portfolio/chart-data` | Your symbols, first purchase per symbol, buy/sell markers, average cost |
| GET | `/api/portfolio/outlook` | Trend, expected, min and max at 1/3/6/12 months per open stock, and each lot's short-term recommendation |
| GET | `/portfolio/operations.csv`, `/portfolio/executions.csv` | Downloads: the operations table; the raw executions |

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

## Portfolio from your trade confirmations

**Importing.** On the **Portfolio** page (or the upload box at the top of the
dashboard) choose or drop your files — as many as you have; the whole folder
is fine. Two kinds are read: broker **trade confirmation PDFs**, and a
**spreadsheet of your orders** (CSV or Excel `.xlsx`) with one row per order
or fill: a date, a ticker, buy/sell (or a negative quantity for a sale), a
quantity and a price, optionally the commission and net amount. English or
Spanish headers work (`fecha, emisora, operación, títulos, precio, comisión`),
dates like `14/10/2025` are read day-first when the header is Spanish or a
day is over 12, and only USD orders are taken (others are listed as skipped).
The app's own "Raw executions CSV" can be read back the same way. For the PDFs: Supported today: the
daily confirmations of GBM's US-dollar account, issued by its clearing firm
DriveWealth (`Confirm_GBMP_….pdf`). Each trade line is read with its own
amounts (principal, commission, fees, net), and the amounts are cross-checked;
a line that looks like a trade but cannot be read is listed in the import
report rather than skipped silently. Re-uploading a file adds nothing (each
execution is recognised), so a monthly re-upload of the whole folder is safe.
The history is kept in `data/app/portfolio/executions.csv` with the account
number masked to its last four characters and no account-holder name.

**How the rows are built.** GBM fills one order as a whole-share and a
fractional execution at the same price; those are merged back into the order
you placed. Sales are matched to purchases **first in, first out**, per stock.
Each row is one purchase lot and what happened to it:

| Column | Meaning |
|---|---|
| Bought, buy price, cost | Trade date, price, and what you paid including the buy commission (pro rata) |
| Sold, sell price, received | For a sold lot: trade date, price, and what you received net of the sell commission |
| Sell / now price, value | For a lot you still hold: the latest price and its value now |
| Gain / loss, return | Received − cost (sold), or value − cost "if sold now" (open, before the sale's commission); green for a gain, red for a loss |

A lot sold in two parts appears twice. A sale with no imported purchase is
shown as "purchase not imported" and flagged, and a few millionths of a share
left by fractional rounding are reported as a residue, not as a position.

**Live prices.** Open lots are priced from Yahoo's latest regular-session
5-minute bar; the page refreshes them every five minutes (with a countdown) and
the server asks Yahoo at most once per five minutes per stock, however many
tabs are open. Prices are in the account's currency (USD).

**Forecast columns and the short-term recommendation (open lots only).** For
each stock you hold, 2,000 possible futures are simulated from its own daily
moves of the last three years (about six years of prices are fetched so that
the longer calls can be back-tested). The stock's past trend is kept only in
proportion to how reliable it is statistically — the average daily move is
multiplied by `max(0, 1 − 1/t²)`, where `t` is its t-statistic — so a weak
trend counts for nothing.

The table keeps its first columns (stock to days held) **frozen** on a
desktop-width screen; the prognosis and the forecasts scroll sideways beneath
them. On a phone only the stock column stays.

**Prognosis.** The verdict for each lot: "waiting looks worthwhile — likely
to rise" or "likely to fall — selling sooner loses less" appears **only when a
trend has passed its back-test** (✓); otherwise "no reliable direction", with
the way the unproven model leans. It also gives the expected (median) price
and the date for it, when the middle scenario gets back to the lot's
break-even price if it is at a loss ("not within a year" when it does not),
and the chance the price halves within a year.

For **1 month, 3 months, 6 months and 1 year**:

| Column | How it is computed |
|---|---|
| Trend | ▲ Up when at least 55% of the futures end above today's price, ▼ Down when at most 45% do, → Flat otherwise (and always Flat when no trend survives the shrinkage). The small print is its record in walk-forward tests on the stock's own past — one test per month, up to ten years of them (about twelve years of prices are fetched). **✓** only when that record beats a coin flip with 95% confidence, where the confidence interval counts overlapping tests only as the independent evidence they are (120 monthly tests of a 1-year call are 10 independent years), and only with at least 8 independent periods. A stock without that much history says how many years the horizon needs ("needs 10 yrs of prices (has 3)"): about 1.7 for 1 month, 3.2 for 3 months, 5.4 for 6 months and 9.9 for 1 year. |
| Expected | The middle outcome (median) at the horizon. The arithmetic average is in the CSV; for volatile stocks a few extreme futures pull it far up. |
| Min / Max | The average lowest and highest close reached along the way. |

**Short-term recommendation (1 month).** Sixty sell prices between today's
and the top of the simulated month are tried; each is scored by its price
improvement over selling today times its chance of being reached within the
month. The best one among those reached in at least a quarter of the futures
is the limit to place — the best trade-off between a higher price and
actually getting it. A protective stop is placed where the price falls in
about one month in ten. Both are shown against what that lot cost you, after
the commission rate you have actually paid. A trend moves this choice only if
it has a ✓ at one month; otherwise the futures carry no trend and the levels
come from volatility alone. If no limit adds at least 0.5% over today's price,
it says sell now.

Computed once per trading day per stock (`data/app/cache/market/outlook/`),
included in the CSV, and available at `/api/portfolio/outlook`. Closing prices
only (intraday swings are wider) and a stop is assumed to fill at its price.
These are statistical estimates from past prices — not a prediction you can
rely on and not financial advice. A validated record is evidence that a
stock's trend persisted in the past decade, which is exactly the kind of
history (a long bull run, a stock you bought because it rose) that need not
continue. On 2026-10-16, two of 20 holdings had validated trends (NVDA and
AVGO, at every horizon); see also [monitoring.md](us_equities/monitoring.md).

**Fundamentals & news for your stocks.** "Analyse my stocks" (Portfolio or
Selection page) runs the stock-selection pipeline on only the tickers you
hold, without the pre-run health check: a few dozen requests to each data
source instead of ~4,800, so it finishes in a minute or two and stays clear of
rate limits. Its results — score, fundamentals, news sentiment, number of
articles, role and flags — are shown on the Portfolio page. Scores there
compare your stocks with each other, so the run never replaces the full
ranking. News needs `NEWSAPI_AI_KEY` in `.env`: without it the only remaining
news source is Yahoo's feed, which blocks many networks, and the page says so.

**Yahoo's limits in the app.** All Yahoo requests from the app go one at a
time, 0.4 s apart; when Yahoo answers empty three times in a row (it does this
when it rate-limits, and yfinance then prints a misleading "possibly
delisted"), live prices pause for five minutes and the last known prices are
shown and marked.

**Charts.** One chart per stock you have traded, starting before your first
purchase, with ▲ at each buy and ▼ at each sale, and your average cost as a
dashed line on positions you still hold — plus the same studies as the
dashboard and a sector view of your stocks against the S&P 500.

**Downloads.** *Download CSV* gives the operations table (including today's
price for open lots); *Raw executions CSV* gives every imported fill.

**Using it elsewhere.** *Use as my holdings* writes your open lots into the
monitor's holdings file (broker option B; the old file is backed up), and the
dashboard's default watchlist becomes your own open positions.

**Limits, stated plainly.** Only the GBM/DriveWealth confirmation layout is
read today. A stock split after a purchase is not applied to the lot (none of
the stocks in the confirmations used to build this split since 2025). Results
are in USD; converting to pesos is not done here.

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
│   ├── market.py        #   prices, indicators, Fibonacci, sector index, live quotes
│   ├── portfolio.py     #   confirmation imports, FIFO operations, live values, CSV
│   └── __init__.py      #   Workflow: one method per user action
├── runner.py            # wraps each job command and records its exit code
├── web/                 # FastAPI: JSON API + server-rendered pages (Jinja2)
│   ├── app.py
│   ├── templates/       #   dashboard, portfolio, selection, allocation, broker, monitor, job
│   └── static/
│       ├── style.css    #   navy / ivory / gold theme, light and dark, responsive
│       ├── app.js       #   draws the charts from /api/market/* (dashboard and portfolio)
│       ├── portfolio.js #   refreshes open-lot prices every five minutes
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
(chart prices and quotes), `data/app/portfolio/` (your imported trade history
and the last import report; "Clear history" moves it to a dated backup), and
the pipelines' usual outputs.

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
