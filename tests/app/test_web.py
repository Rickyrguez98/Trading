"""The web layer: pages, actions, JSON API and the local-only guards.

Jobs are started through a fake process launcher, so no pipeline (and no
network call) actually runs; the tests assert the exact command the app
would run instead.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient  # noqa: E402

from trading_app.services import Workflow, default_paths  # noqa: E402
from trading_app.services.jobs import JobManager  # noqa: E402
from trading_app.web.app import create_app  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


class FakeProc:
    def __init__(self, argv):
        self.argv, self.pid, self.rc = argv, 999_999_999, None

    def poll(self):
        return self.rc


@pytest.fixture
def env(tmp_path):
    shutil.copytree(REPO / "configs/us_equities", tmp_path / "configs/us_equities")
    paths = default_paths(tmp_path)
    launched = []

    def popen(argv, **kw):
        launched.append(FakeProc(argv))
        return launched[-1]

    wf = Workflow(paths, JobManager(paths.jobs_dir, cwd=tmp_path, popen=popen))
    client = TestClient(create_app(wf))
    return client, wf, launched


def _inner(proc):
    """The command the runner wrapper would execute."""
    return proc.argv[proc.argv.index("--") + 1:]


def _ranking(paths):
    paths.ranking_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"rank": [1, 2], "ticker": ["NVDA", "MSFT"], "company_name": ["NVIDIA", "Microsoft"],
                  "final_score": [71.2, 65.0], "fundamentals_score": [62.0, 58.1],
                  "candidate_role": ["core_candidate", "satellite_growth_candidate"],
                  "eligible_for_allocation": [True, True]}).to_csv(paths.ranking_csv, index=False)
    paths.selection_summary.parent.mkdir(parents=True, exist_ok=True)
    paths.selection_summary.write_text(json.dumps({"generated_at": "2026-10-15T01:00:00Z", "run_status": "VALID"}))


def _plan(paths, strategy="balanced"):
    p = paths.plan_csv(strategy)
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"ticker": ["VOO", "NVDA"], "sleeve": ["index_core", "equity_core"],
                  "target_weight_pct": [36.0, 6.0], "realized_weight_pct": [34.4, 5.8],
                  "price": [715.59, 226.3], "shares": [12, 7], "invested_value": [8587.08, 1584.1],
                  "warnings": ["[]", "[]"]}).to_csv(p, index=False)


def _monitor(paths):
    paths.monitor_summary.parent.mkdir(parents=True, exist_ok=True)
    paths.monitor_summary.write_text(json.dumps({
        "generated_at": "2026-10-15T02:00:00Z", "status": "PARTIAL", "reporting_currency": "MXN",
        "status_reasons": ["ranking is 3 days old"],
        "totals": {"market_value": 381476.11, "unrealized_pl": 21805.58},
        "positions": [{"ticker": "NVDA", "market": "SIC", "currency": "MXN", "avg_cost": 3646.98,
                       "first_purchase_date": "2026-06-15", "valuation": {"unrealized_pl_pct": 7.3},
                       "recommendation": {"action": "HOLD", "shares_delta": 0, "value_delta_reporting": 0,
                                          "current_weight_pct": 6.1, "thesis_status": "INTACT",
                                          "confidence": "medium", "rule": "thesis intact; within band",
                                          "composite_score": 2.75, "outlook": "37% chance ...",
                                          "evidence": [{"factor": "trend", "score": 1.0, "summary": "uptrend"}],
                                          "blockers": [], "review_by": "2026-11-19"}}]}))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["/", "/selection", "/allocation", "/broker", "/monitor"])
def test_every_page_renders_on_an_empty_project(env, url):
    client, _, _ = env
    r = client.get(url)
    assert r.status_code == 200 and "not financial advice" in r.text


def test_pages_show_the_pipeline_outputs(env):
    client, wf, _ = env
    _ranking(wf.paths), _plan(wf.paths), _monitor(wf.paths)
    assert "NVIDIA" in client.get("/selection").text
    alloc = client.get("/allocation?strategy=balanced").text
    assert "VOO" in alloc and "8,587.08" in alloc
    mon = client.get("/monitor").text
    assert "thesis intact; within band" in mon and "uptrend" in mon
    dash = client.get("/").text
    assert "2 ranked stocks" in dash and "1 HOLD" in dash


def test_outputs_are_html_escaped(env):
    client, wf, _ = env
    _ranking(wf.paths)
    df = pd.read_csv(wf.paths.ranking_csv)
    df.loc[0, "company_name"] = "<script>alert(1)</script>"
    df.to_csv(wf.paths.ranking_csv, index=False)
    page = client.get("/selection").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def test_broker_option_init_and_upload(env):
    client, wf, _ = env
    r = client.post("/broker/option", data={"option": "C"}, follow_redirects=False)
    assert r.status_code == 303 and wf.settings.broker_option == "C"
    assert "error" in client.post("/broker/option", data={"option": "Z"}, follow_redirects=False).headers["location"]
    client.post("/broker/option", data={"option": "B"})
    client.post("/broker/init")
    assert "WALMEX" in client.get("/broker").text                    # the template rows
    csv = b"ticker,shares,avg_cost,currency\nMSFT,3,410,USD\n"
    r = client.post("/broker/upload", files={"file": ("mine.csv", csv, "text/csv")}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert wf.paths.resolve(wf.settings.holdings_csv).read_bytes() == csv
    bad = client.post("/broker/upload", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")}, follow_redirects=False)
    assert "error=" in bad.headers["location"]


def test_full_selection_runs_the_documented_command(env):
    client, _, launched = env
    r = client.post("/selection/run", data={"mode": "full"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    cmd = _inner(launched[-1])
    assert cmd[1:3] == ["-m", "asset_selection.pipelines.run_asset_selection"] and "--top" in cmd
    assert client.get(r.headers["location"]).status_code == 200


def test_a_second_selection_is_refused_while_one_runs(env):
    client, _, _ = env
    client.post("/selection/run", data={"mode": "full"})
    r = client.post("/selection/run", data={"mode": "sample", "limit": "20"}, follow_redirects=False)
    assert "already%20running" in r.headers["location"]


def test_sample_selection_stays_in_its_job_folder(env):
    client, wf, launched = env
    client.post("/selection/run", data={"mode": "sample", "limit": "20"})
    cmd = _inner(launched[-1])
    assert str(wf.paths.jobs_dir) in cmd[cmd.index("--processed-dir") + 1]


def test_allocation_validates_and_runs(env):
    client, _, launched = env
    bad = client.post("/allocation/run", data={"amount": "-3"}, follow_redirects=False)
    assert "error=" in bad.headers["location"] and not launched
    client.post("/allocation/run", data={"amount": "25000", "strategy": "balanced", "offline": "1"})
    cmd = _inner(launched[-1])
    assert cmd[cmd.index("--amount") + 1] == "25000.00" and "--offline" in cmd


def test_monitor_with_an_uploaded_statement(env):
    client, wf, launched = env
    pdf = (REPO / "tests/fixtures/monitoring/gbm_statement.pdf").read_bytes()
    r = client.post("/monitor/run", data={"option": "A", "cash": "5000"},
                    files={"file": ("estado.pdf", pdf, "application/pdf")}, follow_redirects=False)
    assert r.headers["location"].startswith("/jobs/")
    cmd = _inner(launched[-1])
    held = Path(cmd[cmd.index("--holdings") + 1])
    assert held.parent == wf.paths.uploads_dir and held.read_bytes() == pdf
    assert cmd[cmd.index("--broker") + 1] == "A" and cmd[cmd.index("--cash") + 1] == "5000.00"


def test_monitor_refuses_without_holdings(env):
    client, _, launched = env
    r = client.post("/monitor/run", data={"option": "B"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and not launched


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

def test_api_status_and_reads(env):
    client, wf, _ = env
    _ranking(wf.paths), _plan(wf.paths), _monitor(wf.paths)
    s = client.get("/api/status").json()
    assert set(s) == {"ranking", "plans", "broker", "monitor", "jobs"}
    assert s["ranking"]["rows"] == 2 and s["monitor"]["actions"] == {"HOLD": 1}
    assert [r["ticker"] for r in client.get("/api/ranking?top=1").json()["rows"]] == ["NVDA"]
    assert client.get("/api/plans/balanced").json()["rows"][0]["ticker"] == "VOO"
    assert client.get("/api/plans/../../etc").status_code == 404
    assert client.get("/api/monitor/latest").json()["actions"] == {"HOLD": 1}


def test_api_jobs(env):
    client, _, _ = env
    assert client.post("/api/jobs/selection", json={"mode": "sample", "limit": "many"}).status_code == 400
    r = client.post("/api/jobs/selection", json={"mode": "sample", "limit": 20})
    assert r.status_code == 202 and r.json()["state"] == "running"
    assert client.post("/api/jobs/selection", json={"mode": "full"}).status_code == 409
    assert client.post("/api/jobs/allocation", json={"amount": "abc"}).status_code == 400
    assert client.post("/api/jobs/nonsense", json={}).status_code == 404
    # Malformed values are a 400 with a message, never a server error.
    assert client.post("/api/jobs/allocation", json={"amount": 100, "instrument_prices": {"VOO": "x"}}).status_code == 400
    assert client.post("/api/jobs/allocation", json={"amount": 100, "instrument_prices": ["VOO"]}).status_code == 400
    assert client.post("/api/jobs/monitor", json={"option": 5}).status_code == 400
    job_id = r.json()["id"]
    assert client.get(f"/api/jobs/{job_id}").json()["id"] == job_id
    assert client.get("/api/jobs/..%2F..%2Fetc").status_code == 404
    assert client.put("/api/broker", json={"option": "A"}).json() == {"selected": "A"}
    assert client.put("/api/broker", json={"option": "Q"}).status_code == 400


# ---------------------------------------------------------------------------
# Local-only guards
# ---------------------------------------------------------------------------

def test_foreign_host_header_is_rejected(env):
    client, _, _ = env
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_cross_site_writes_are_refused(env):
    client, wf, launched = env
    r = client.post("/selection/run", data={"mode": "full"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and not launched
    r = client.put("/api/broker", json={"option": "C"}, headers={"referer": "https://evil.example/x"})
    assert r.status_code == 403 and wf.settings.broker_option == "B"
    ok = client.post("/broker/option", data={"option": "A"}, headers={"origin": "http://testserver"},
                     follow_redirects=False)
    assert ok.status_code == 303


def test_cli_refuses_non_loopback_and_wrong_folders(tmp_path, capsys):
    from trading_app.cli import main
    assert main(["--host", "0.0.0.0", "--no-browser"]) == 2
    assert "non-loopback" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "--no-browser"]) == 2
    assert "does not look like the Trading project" in capsys.readouterr().err
