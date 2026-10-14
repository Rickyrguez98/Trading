"""FastAPI application: a JSON API under ``/api`` and a server-rendered dashboard.

It is a local, single-user app. It binds to 127.0.0.1 (see :mod:`trading_app.cli`),
and two guards stop other websites from driving it through your browser:
requests must carry a localhost ``Host`` header (DNS rebinding), and any form
post or API write must come from this same origin (cross-site requests).
Nothing here places orders or accepts broker credentials.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..services import STRATEGIES, InvalidRequest, JobConflict, Workflow, safe_init_holdings, status
from ..services.jobs import Job

HERE = Path(__file__).resolve().parent
LOCAL_HOSTS = ["127.0.0.1", "localhost", "testserver"]
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def create_app(workflow: Optional[Workflow] = None) -> FastAPI:
    wf = workflow or Workflow()
    app = FastAPI(title="Trading app", version=_version(), docs_url="/api/docs", redoc_url=None,
                  description="Local API over selection, allocation, broker options and the monitor. "
                              "Research output only; no orders are placed.")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(money=_money, pct=_pct, num=_num)
    app.state.workflow = wf

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method in _UNSAFE:
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).hostname != request.url.hostname:
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        return await call_next(request)

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg"))
        ctx.setdefault("error", request.query_params.get("error"))
        return templates.TemplateResponse(request, name, {"active": name.split(".")[0], **ctx})

    def back(url: str, *, msg: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
        q = f"?msg={quote(msg)}" if msg else (f"?error={quote(error)}" if error else "")
        return RedirectResponse(url + q, status_code=303)

    def started(action, failure_url: str) -> RedirectResponse:
        try:
            job: Job = action()
        except (InvalidRequest, JobConflict, ValueError) as exc:
            return back(failure_url, error=str(exc))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return page(request, "dashboard.html", d=wf.dashboard())

    @app.get("/selection", response_class=HTMLResponse)
    def selection(request: Request):
        return page(request, "selection.html", ranking=status.ranking_info(wf.paths),
                    rows=status.top_ranking(wf.paths, 25), running=wf.jobs.running("selection"))

    @app.post("/selection/run")
    def selection_run(mode: str = Form("full"), limit: int = Form(50)):
        return started(lambda: wf.start_selection(mode, limit if mode == "sample" else None), "/selection")

    @app.get("/allocation", response_class=HTMLResponse)
    def allocation(request: Request, strategy: Optional[str] = None):
        plans = status.plan_infos(wf.paths)
        chosen = strategy if strategy in STRATEGIES else (plans[0].strategy if plans else None)
        return page(request, "allocation.html", plans=plans, chosen=chosen, strategies=STRATEGIES,
                    rows=status.plan_rows(wf.paths, chosen) if chosen else [],
                    summary=status.allocation_summary(wf.paths), ranking=status.ranking_info(wf.paths),
                    running=wf.jobs.running("allocation"))

    @app.post("/allocation/run")
    def allocation_run(amount: str = Form(...), strategy: str = Form(""), offline: Optional[str] = Form(None)):
        return started(lambda: wf.start_allocation(amount, strategy or None, offline=bool(offline)),
                       "/allocation")

    @app.get("/broker", response_class=HTMLResponse)
    def broker(request: Request):
        s = wf.settings
        return page(request, "broker.html", broker=status.broker_status(wf.paths, s),
                    holdings=status.holdings_rows(wf.paths, s), settings=s)

    @app.post("/broker/option")
    def broker_option(option: str = Form(...)):
        try:
            s = wf.set_broker_option(option)
        except ValueError as exc:
            return back("/broker", error=str(exc))
        return back("/broker", msg=f"Broker option {s.broker_option} selected")

    @app.post("/broker/init")
    def broker_init():
        try:
            return back("/broker", msg=safe_init_holdings(wf))
        except InvalidRequest as exc:
            return back("/broker", error=str(exc))

    @app.post("/broker/upload")
    async def broker_upload(file: UploadFile = File(...)):
        try:
            dest = wf.replace_holdings(await file.read())
        except InvalidRequest as exc:
            return back("/broker", error=str(exc))
        return back("/broker", msg=f"Holdings replaced ({dest.name}); the previous file was backed up")

    @app.get("/monitor", response_class=HTMLResponse)
    def monitor(request: Request):
        return page(request, "monitor.html", view=status.latest_monitor(wf.paths, wf.jobs),
                    settings=wf.settings, plans=status.plan_infos(wf.paths),
                    ranking=status.ranking_info(wf.paths), running=wf.jobs.running("monitor"),
                    chronos=importlib.util.find_spec("chronos") is not None)

    @app.post("/monitor/run")
    async def monitor_run(option: str = Form(""), target: str = Form(""), cash: str = Form("0"),
                          forecast: str = Form("bootstrap"), refresh: Optional[str] = Form(None),
                          file: Optional[UploadFile] = File(None)):
        try:
            upload = None
            if file is not None and file.filename:
                upload = wf.save_upload(file.filename, await file.read())
            cash_v = float(cash or 0)
        except (InvalidRequest, ValueError) as exc:
            return back("/monitor", error=str(exc))
        return started(lambda: wf.start_monitor(option=option or None, upload=upload,
                                                target_strategy=target or None, cash=cash_v,
                                                forecast=forecast, refresh_signals=bool(refresh)),
                       "/monitor")

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = _job_or_404(wf, job_id)
        return page(request, "job.html", job=job, log=wf.jobs.log_tail(job.id, 60))

    # -------------------------------------------------------------------- API
    @app.get("/api/status")
    def api_status() -> Dict[str, Any]:
        d = wf.dashboard()
        mon = d["monitor"]
        return {
            "ranking": d["ranking"].__dict__,
            "plans": [p.__dict__ for p in d["plans"]],
            "broker": d["broker"],
            "monitor": None if mon is None else {"generated_at": mon.generated_at, "source": mon.source,
                                                 "actions": mon.actions, "status": mon.summary.get("status")},
            "jobs": [_job_dict(wf, j) for j in d["jobs"]],
        }

    @app.get("/api/ranking")
    def api_ranking(top: int = 25):
        return {"ranking": status.ranking_info(wf.paths).__dict__,
                "rows": status.top_ranking(wf.paths, max(1, min(int(top), 500)))}

    @app.get("/api/plans/{strategy}")
    def api_plan(strategy: str):
        if strategy not in STRATEGIES:
            raise HTTPException(404, "unknown strategy")
        return {"strategy": strategy, "rows": status.plan_rows(wf.paths, strategy)}

    @app.get("/api/broker")
    def api_broker():
        return status.broker_status(wf.paths, wf.settings)

    @app.put("/api/broker")
    def api_broker_set(body: Dict[str, str]):
        try:
            return {"selected": wf.set_broker_option(body.get("option", "")).broker_option}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/monitor/latest")
    def api_monitor_latest():
        view = status.latest_monitor(wf.paths, wf.jobs)
        if view is None:
            raise HTTPException(404, "no monitor result yet")
        return {"source": view.source, "actions": view.actions, "summary": view.summary}

    @app.get("/api/jobs")
    def api_jobs():
        return [_job_dict(wf, j) for j in wf.jobs.list(limit=50)]

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str):
        job = _job_or_404(wf, job_id)
        return {**_job_dict(wf, job), "log_tail": wf.jobs.log_tail(job.id, 60)}

    @app.post("/api/jobs/{kind}", status_code=202)
    def api_start(kind: str, body: Dict[str, Any]):
        actions = {
            "selection": lambda: wf.start_selection(body.get("mode", "full"), body.get("limit")),
            "allocation": lambda: wf.start_allocation(body.get("amount"), body.get("strategy"),
                                                      offline=bool(body.get("offline")),
                                                      instrument_prices=body.get("instrument_prices")),
            "monitor": lambda: wf.start_monitor(option=body.get("option"),
                                                target_strategy=body.get("target_strategy"),
                                                cash=body.get("cash") or 0.0,
                                                forecast=body.get("forecast", "bootstrap"),
                                                refresh_signals=bool(body.get("refresh_signals"))),
        }
        if kind not in actions:
            raise HTTPException(404, "unknown job kind")
        try:
            return _job_dict(wf, actions[kind]())
        except InvalidRequest as exc:
            raise HTTPException(400, str(exc)) from None
        except JobConflict as exc:
            raise HTTPException(409, str(exc)) from None

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("asset-selection")
    except PackageNotFoundError:
        return "dev"


def _job_or_404(wf: Workflow, job_id: str) -> Job:
    if not job_id.replace("-", "").isalnum():
        raise HTTPException(404, "no such job")
    job = wf.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job


def _job_dict(wf: Workflow, job: Job) -> Dict[str, Any]:
    return {"id": job.id, "kind": job.kind, "title": job.title, "state": job.state,
            "created_at": job.created_at, "finished_at": job.finished_at,
            "returncode": job.returncode, "command": job.command, "outputs": job.outputs,
            "params": job.params}


def _money(x: Any, nd: int = 2) -> str:
    if isinstance(x, Undefined):
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.{nd}f}"


def _pct(x: Any, nd: int = 1, signed: bool = False) -> str:
    if isinstance(x, Undefined):
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{v:+.{nd}f}%" if signed else f"{v:.{nd}f}%"


def _num(x: Any, nd: int = 1) -> str:
    if isinstance(x, Undefined):
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.{nd}f}"
