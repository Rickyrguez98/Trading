"""The app's service layer: commands, jobs, settings and status readers.

Plain Python -- runs on every supported interpreter, no web framework needed.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

from trading_app.services import InvalidRequest, JobConflict, Workflow, default_paths
from trading_app.services import commands, status
from trading_app.services.jobs import FAILED, SUCCEEDED, JobManager
from trading_app.services.settings import AppSettings, load_settings, save_settings

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def project(tmp_path):
    """A throwaway project root with the real configs and template."""
    for rel in ("configs/us_equities",):
        shutil.copytree(REPO / rel, tmp_path / rel)
    return default_paths(tmp_path)


@pytest.fixture
def wf(project):
    return Workflow(project)


def _wait(jobs, job_id, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        job = jobs.get(job_id)
        if job.done:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def test_full_selection_writes_the_canonical_ranking(project):
    argv = commands.selection_command(project, mode="full")
    assert argv[:3] == [sys.executable, "-m", commands.SELECTION_MODULE]
    assert "--processed-dir" not in argv and "--universe" not in argv


def test_sample_selection_can_never_overwrite_the_main_ranking(project, tmp_path):
    job_dir = tmp_path / "job"
    argv = commands.selection_command(project, mode="sample", limit=40, job_dir=job_dir)
    assert argv[argv.index("--processed-dir") + 1] == str(job_dir / "processed")
    assert argv[argv.index("--output-dir") + 1] == str(job_dir / "reports")
    assert argv[argv.index("--limit") + 1] == "40"


@pytest.mark.parametrize("kw", [{"mode": "everything"}, {"mode": "sample", "limit": 2},
                                {"mode": "sample", "limit": 10_000}])
def test_bad_selection_requests(project, tmp_path, kw):
    with pytest.raises(InvalidRequest):
        commands.selection_command(project, job_dir=tmp_path, **kw)


def test_allocation_command(project):
    argv = commands.allocation_command(project, amount="25000", strategy="balanced", offline=True,
                                       instrument_prices={"voo": 715.59, "BND": 70.22})
    assert argv[argv.index("--amount") + 1] == "25000.00"
    assert "--offline" in argv and argv[argv.index("--strategy") + 1] == "balanced"
    i = argv.index("--instrument-price")
    assert argv[i + 1:i + 3] == ["BND=70.22", "VOO=715.59"]          # one flag, several values


@pytest.mark.parametrize("kw", [{"amount": -5}, {"amount": "lots"}, {"amount": 100, "strategy": "yolo"},
                                {"amount": 100, "instrument_prices": {"VOO; rm -rf": 1}},
                                {"amount": 100, "instrument_prices": {"VOO": 0}}])
def test_bad_allocation_requests(project, kw):
    with pytest.raises(InvalidRequest):
        commands.allocation_command(project, **kw)


def test_monitor_command(project, tmp_path):
    plan = project.plan_csv("balanced")
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("ticker,realized_weight_pct\nVOO,36\n")
    argv = commands.monitor_command(project, option="B", output_dir=tmp_path, holdings=Path("h.csv"),
                                    target_strategy="balanced", cash=5000, forecast="chronos")
    assert argv[argv.index("--broker") + 1] == "B" and argv[argv.index("--holdings") + 1] == "h.csv"
    assert argv[argv.index("--target") + 1] == str(plan) and argv[argv.index("--cash") + 1] == "5000.00"
    c = commands.monitor_command(project, option="C", output_dir=tmp_path, holdings=Path("ignored.csv"))
    assert "--holdings" not in c                                      # option C never takes a file


@pytest.mark.parametrize("kw", [{"option": "D"}, {"option": "B", "forecast": "crystal-ball"},
                                {"option": "B", "cash": -1}, {"option": "B", "target_strategy": "balanced"}])
def test_bad_monitor_requests(project, tmp_path, kw):
    with pytest.raises(InvalidRequest):
        commands.monitor_command(project, output_dir=tmp_path, **kw)


# ---------------------------------------------------------------------------
# Jobs (real subprocesses)
# ---------------------------------------------------------------------------

def test_a_job_runs_records_its_exit_code_and_log(tmp_path):
    jobs = JobManager(tmp_path / "jobs", cwd=tmp_path)
    job = jobs.start("selection", "hello", [sys.executable, "-c", "print('hello from the job')"])
    done = _wait(jobs, job.id)
    assert done.state == SUCCEEDED and done.returncode == 0
    assert "hello from the job" in jobs.log_tail(job.id)
    assert (jobs.job_dir(job.id) / "exit_code").read_text() == "0"


def test_a_failing_job_is_reported_as_failed(tmp_path):
    jobs = JobManager(tmp_path / "jobs", cwd=tmp_path)
    job = jobs.start("allocation", "boom", [sys.executable, "-c", "import sys; sys.exit(3)"])
    done = _wait(jobs, job.id)
    assert done.state == FAILED and done.returncode == 3


def test_outcome_survives_an_app_restart(tmp_path):
    first = JobManager(tmp_path / "jobs", cwd=tmp_path)
    job = first.start("monitor", "quick", [sys.executable, "-c", "pass"])
    _wait(first, job.id)
    reopened = JobManager(tmp_path / "jobs", cwd=tmp_path)       # no in-memory process handle
    assert reopened.get(job.id).state == SUCCEEDED


def test_one_job_of_a_kind_at_a_time(tmp_path):
    jobs = JobManager(tmp_path / "jobs", cwd=tmp_path)
    slow = jobs.start("selection", "slow", [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(JobConflict):
            jobs.start("selection", "second", [sys.executable, "-c", "pass"])
        other = jobs.start("monitor", "different kind", [sys.executable, "-c", "pass"])
        assert _wait(jobs, other.id).state == SUCCEEDED
    finally:
        jobs._procs[slow.id].kill()


def test_log_tail_keeps_the_latest_progress_frame(tmp_path):
    jobs = JobManager(tmp_path / "jobs", cwd=tmp_path)
    code = "import sys; sys.stdout.write('Stage 2: 10%\\rStage 2: 50%\\rStage 2: 100%\\ndone\\n')"
    job = jobs.start("selection", "bars", [sys.executable, "-c", code])
    _wait(jobs, job.id)
    assert jobs.log_tail(job.id).splitlines() == ["Stage 2: 100%", "done"]


def test_jobs_are_listed_newest_first(tmp_path):
    jobs = JobManager(tmp_path / "jobs", cwd=tmp_path)
    a = jobs.start("allocation", "a", [sys.executable, "-c", "pass"], job_id="20260101-000000-aaaaaa")
    b = jobs.start("monitor", "b", [sys.executable, "-c", "pass"], job_id="20260102-000000-bbbbbb")
    assert [j.id for j in jobs.list()] == [b.id, a.id]
    assert jobs.get("nope") is None


def test_job_command_is_shown_without_the_interpreter_path(tmp_path):
    jobs = JobManager(tmp_path / "jobs", cwd=tmp_path)
    job = jobs.start("selection", "x", [sys.executable, "-m", "json.tool", "--help"])
    assert job.command.startswith("python -m json.tool")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_default_to_the_monitoring_config_then_persist(project):
    assert load_settings(project).broker_option == "B"
    save_settings(project, AppSettings(broker_option="C"))
    assert load_settings(project).broker_option == "C"
    with pytest.raises(ValueError):
        save_settings(project, AppSettings(broker_option="Z"))


# ---------------------------------------------------------------------------
# Status readers
# ---------------------------------------------------------------------------

def _write_ranking(project, generated_at, run_status="VALID"):
    project.ranking_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"rank": [2, 1], "ticker": ["MSFT", "NVDA"], "final_score": [70.0, None],
                  "candidate_role": ["core_candidate", "core_candidate"]}).to_csv(project.ranking_csv, index=False)
    project.selection_summary.parent.mkdir(parents=True, exist_ok=True)
    project.selection_summary.write_text(json.dumps({"generated_at": generated_at, "run_status": run_status}))


def test_ranking_info_and_staleness(project):
    assert not status.ranking_info(project).exists
    _write_ranking(project, "2026-01-01T00:00:00Z")
    info = status.ranking_info(project)
    assert info.exists and info.rows == 2 and info.run_status == "VALID" and info.stale
    rows = status.top_ranking(project, n=5)
    assert [r["ticker"] for r in rows] == ["NVDA", "MSFT"] and rows[0]["final_score"] is None


def test_plans_and_rows(project):
    p = project.plan_csv("aggressive")
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"ticker": ["VOO", "NVDA"], "sleeve": ["index_core", "equity_core"],
                  "shares": [3, 0], "invested_value": [2100.0, 0.0]}).to_csv(p, index=False)
    (info,) = status.plan_infos(project)
    assert info.strategy == "aggressive" and info.positions == 1 and info.invested == 2100.0
    assert [r["ticker"] for r in status.plan_rows(project, "aggressive")] == ["VOO", "NVDA"]
    assert status.plan_rows(project, "../../etc") == []


def test_broker_status_marks_the_selected_option(project):
    st = status.broker_status(project, AppSettings(broker_option="A"))
    assert st["selected"] == "A"
    assert [o["code"] for o in st["options"] if o["selected"]] == ["A"]


def test_latest_monitor_prefers_the_newest_result(project, wf):
    project.monitor_summary.parent.mkdir(parents=True, exist_ok=True)
    project.monitor_summary.write_text(json.dumps({
        "generated_at": "2026-10-01T00:00:00Z",
        "positions": [{"recommendation": {"action": "HOLD"}}, {"recommendation": {"action": "SELL"}}]}))
    view = status.latest_monitor(project, wf.jobs)
    assert view.actions == {"HOLD": 1, "SELL": 1} and view.source.endswith(".json")


# ---------------------------------------------------------------------------
# Workflow actions
# ---------------------------------------------------------------------------

def test_init_and_replace_holdings_keep_a_backup(wf, project):
    created = wf.init_holdings()
    assert created.exists()
    new = b"ticker,shares,avg_cost,currency\nMSFT,3,410,USD\n"
    dest = wf.replace_holdings(new)
    assert dest.read_bytes() == new
    backups = list(dest.parent.glob("my_holdings.backup-*.csv"))
    assert len(backups) == 1


def test_replace_holdings_refuses_an_unusable_csv(wf):
    with pytest.raises(InvalidRequest, match="cannot be used"):
        wf.replace_holdings(b"name,age\nbob,3\n")


@pytest.mark.parametrize("name, data", [("x.exe", b"MZ"), ("x.pdf", b"not a pdf"), ("x.csv", b"")])
def test_uploads_are_checked(wf, name, data):
    with pytest.raises(InvalidRequest):
        wf.save_upload(name, data)


def test_monitor_needs_holdings_for_option_b(wf):
    with pytest.raises(InvalidRequest, match="no holdings file"):
        wf.start_monitor(option="B")


def test_monitor_option_a_needs_a_pdf(wf):
    with pytest.raises(InvalidRequest, match="statement PDF"):
        wf.start_monitor(option="A")


def test_set_broker_option(wf):
    assert wf.set_broker_option("c").broker_option == "C"
    assert wf.settings.broker_option == "C"
