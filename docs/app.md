# Trading app

A local web app over the whole US-equities workflow — select, allocate, choose
a broker option, monitor — so the project can be used without the command
line. It runs on your machine at `http://127.0.0.1:8765`, has no login, and is
only reachable from your own computer. It never places orders.

> **Status: first structure.** Every page works end to end, but this is the
> foundation, not the finished product — see [Roadmap](#roadmap).

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
| **Dashboard** | See the state of all four steps: ranking age and run status, plans, broker option readiness, the latest monitor result, recent jobs. |
| **1 · Select** | Start the full-universe selection (replaces the ranking every step reads) or a quick sample run (kept in its own job folder — sample scores are relative to the sample and must never replace the full ranking). Top 25 table. |
| **2 · Allocate** | Enter an amount and a strategy; see each plan's positions in percent and money. |
| **3 · Broker** | Pick option A, B or C (saved in `data/app/settings.json`), see what each needs, create your holdings CSV from the template, or replace it with an upload (the old file is backed up). |
| **4 · Monitor** | Run the monitor with the chosen option (upload a statement PDF for A), an optional target plan and new cash; read the actions, the evidence for each and the outlook. |
| **Job** | Live log of a running job; refreshes every 3 seconds; links to the result. |
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

Starting a job returns `202` with the job; a second job of the same kind while
one runs returns `409`; invalid input returns `400`.

## Architecture

```
src/trading_app/
├── services/            # plain Python, no web framework — the reusable core
│   ├── paths.py         #   every file the pipelines read and write
│   ├── commands.py      #   validated argument lists for the three pipeline CLIs
│   ├── jobs.py          #   background jobs: subprocess, log, state on disk
│   ├── status.py        #   readers: ranking, plans, broker readiness, monitor results
│   ├── settings.py      #   app preferences (data/app/settings.json)
│   └── __init__.py      #   Workflow: one method per user action
├── runner.py            # wraps each job command and records its exit code
├── web/                 # FastAPI: JSON API + server-rendered pages (Jinja2)
│   ├── app.py
│   ├── templates/       #   dashboard, selection, allocation, broker, monitor, job
│   └── static/style.css #   light/dark, responsive
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

All under `data/`, which git ignores: `data/app/settings.json`,
`data/app/jobs/`, `data/app/uploads/`, and the pipelines' usual outputs.

## Tests

```bash
pytest tests/app        # services on any Python; web tests need the app extra (3.10+)
```

Web tests start jobs through a fake process launcher, so no pipeline or network
call runs; they assert the exact command the app would run, the pages, the API,
HTML escaping, and both local-only guards.

## Roadmap

- **Charts**: ranking score distribution, allocation sleeves, P/L history per
  position (each monitor run started from the app is kept in its job folder,
  so history accumulates; CLI runs overwrite the latest summary).
- **Holdings editor** in the browser for option B (today: upload or edit the CSV).
- **Scheduling**: a monthly selection run and a weekly monitor run, with a
  notification when a SELL/TRIM appears.
- **Monitor history**: compare this month's recommendations with last month's.
- **Desktop packaging**: a double-click launcher instead of a terminal command.
- **If it is ever exposed beyond your laptop**: add authentication first; the
  current guards assume a single local user.
