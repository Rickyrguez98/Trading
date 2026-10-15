"""Broker options A/B/C and the read-only IBKR Flex connector (synthetic responses)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from asset_selection.monitoring.broker import (
    OPTIONS,
    BrokerConfigError,
    infer_option,
    init_holdings,
    load_holdings,
    readiness,
    resolve_option,
)
from asset_selection.monitoring.config import BrokerConfig, load_config
from asset_selection.monitoring.ibkr_flex import (
    FlexCredentials,
    IbkrFlexError,
    fetch_statement_xml,
    parse_open_positions,
)

TOKEN = "123456789012345678901234"          # synthetic
QUERY = "987654"

SEND_OK = (b'<FlexStatementResponse timestamp="15 October, 2026 09:30 AM EDT">'
           b"<Status>Success</Status><ReferenceCode>5555</ReferenceCode>"
           b"<Url>https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>"
           b"</FlexStatementResponse>")
IN_PROGRESS = (b'<FlexStatementResponse timestamp="15 October, 2026 09:30 AM EDT"><Status>Warn</Status>'
               b"<ErrorCode>1019</ErrorCode><ErrorMessage>Statement generation in progress. "
               b"Please try again shortly.</ErrorMessage></FlexStatementResponse>")
EXPIRED = (b'<FlexStatementResponse timestamp="15 October, 2026 09:30 AM EDT"><Status>Fail</Status>'
           b"<ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired.</ErrorMessage>"
           b"</FlexStatementResponse>")

STATEMENT = b"""<FlexQueryResponse queryName="positions" type="AF">
<FlexStatements count="1"><FlexStatement accountId="U0000000" fromDate="20261014" toDate="20261014">
<OpenPositions>
  <OpenPosition accountId="U0000000" assetCategory="STK" symbol="NVDA" currency="USD" listingExchange="NASDAQ"
    position="10" costBasisPrice="190.5" costBasisMoney="1905" markPrice="226.32" levelOfDetail="SUMMARY"
    reportDate="20261014" side="Long"/>
  <OpenPosition accountId="U0000000" assetCategory="STK" symbol="NVDA" currency="USD" listingExchange="NASDAQ"
    position="6" costBasisPrice="180" markPrice="226.32" levelOfDetail="LOT" openDateTime="20250314;093512"
    reportDate="20261014" side="Long"/>
  <OpenPosition accountId="U0000000" assetCategory="STK" symbol="NVDA" currency="USD" listingExchange="NASDAQ"
    position="4" costBasisPrice="206.25" markPrice="226.32" levelOfDetail="LOT" openDateTime="2025-09-02;10:01:00"
    reportDate="20261014" side="Long"/>
  <OpenPosition accountId="U0000000" assetCategory="STK" symbol="BRK B" currency="USD" listingExchange="NYSE"
    position="2" costBasisPrice="" costBasisMoney="900" levelOfDetail="SUMMARY" reportDate="20261014" side="Long"/>
  <OpenPosition accountId="U0000000" assetCategory="STK" symbol="WALMEX" currency="MXN" listingExchange="MEXI"
    position="40" costBasisPrice="62.15" levelOfDetail="SUMMARY" reportDate="20261014" side="Long"/>
  <OpenPosition accountId="U0000000" assetCategory="OPT" symbol="AAPL  261120C00350000" currency="USD"
    position="1" costBasisPrice="5.2" levelOfDetail="SUMMARY" reportDate="20261014" side="Long"/>
  <OpenPosition accountId="U0000000" assetCategory="STK" symbol="TSLA" currency="USD" position="-3"
    costBasisPrice="250" levelOfDetail="SUMMARY" reportDate="20261014" side="Short"/>
  <OpenPosition accountId="U0000000" assetCategory="STK" symbol="SAP" currency="EUR" position="5"
    costBasisPrice="180" levelOfDetail="SUMMARY" reportDate="20261014" side="Long"/>
</OpenPositions>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Server Error for url: "
                                     f"https://ndcdyn.interactivebrokers.com/x?t={TOKEN}&q={QUERY}&v=3")


class _Http:
    """Scripted IBKR: replies in order and records what was sent where."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        reply = self.replies.pop(0)
        return reply if isinstance(reply, _Resp) else _Resp(reply)


CREDS = FlexCredentials(TOKEN, QUERY)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, code", [
    ("A", "A"), ("pdf", "A"), ("statement_pdf", "A"),
    ("b", "B"), ("csv", "B"),
    ("C", "C"), ("ibkr", "C"), ("ibkr_flex", "C"),
])
def test_resolve_option(value, code):
    assert resolve_option(value).code == code


def test_unknown_option_is_rejected():
    with pytest.raises(BrokerConfigError, match="A .* B .* C"):
        resolve_option("robinhood")


def test_holdings_path_implies_its_option():
    assert infer_option("estado.PDF").code == "A"
    assert infer_option("mine.csv").code == "B"
    assert infer_option(None) is None


def test_shipped_config_selects_option_b_and_keeps_holdings_out_of_git():
    cfg = load_config("configs/us_equities/monitoring.yaml")
    assert resolve_option(cfg.broker.option).code == "B"
    assert cfg.broker.holdings_csv.startswith("data/")       # data/ is git-ignored


def test_config_rejects_an_unknown_broker():
    from asset_selection.monitoring.config import MonitoringConfigError
    with pytest.raises(MonitoringConfigError, match="broker.option"):
        load_config({"broker": {"option": "Z"}})


def test_readiness_reports_each_option(tmp_path, monkeypatch):
    monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IBKR_FLEX_QUERY_ID", raising=False)
    csv = tmp_path / "mine.csv"
    csv.write_text("ticker,shares,avg_cost\nAAPL,1,100\n")
    state = {r.option.code: r for r in readiness(BrokerConfig(holdings_csv=str(csv)))}
    assert state["B"].ready and not state["A"].ready
    assert not state["C"].ready and "IBKR_FLEX_TOKEN" in state["C"].detail
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "x")
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "y")
    assert {r.option.code: r for r in readiness(BrokerConfig())}["C"].ready


def test_init_holdings_copies_the_template_and_never_overwrites(tmp_path):
    cfg = BrokerConfig(holdings_csv=str(tmp_path / "h" / "my_holdings.csv"))
    created = init_holdings(cfg)
    assert created.read_text() == Path("configs/us_equities/holdings_template.csv").read_text()
    with pytest.raises(BrokerConfigError, match="already exists"):
        init_holdings(cfg)


def test_option_and_file_type_must_agree(tmp_path):
    with pytest.raises(BrokerConfigError, match="not a .pdf"):
        load_holdings(OPTIONS["A"], BrokerConfig(), path="configs/us_equities/holdings_template.csv",
                      processed_dir=tmp_path)
    with pytest.raises(BrokerConfigError, match="option A"):
        load_holdings(OPTIONS["B"], BrokerConfig(), path="tests/fixtures/monitoring/gbm_statement.pdf",
                      processed_dir=tmp_path)


def test_option_b_loads_the_csv(tmp_path):
    loaded = load_holdings(OPTIONS["B"], BrokerConfig(holdings_csv="configs/us_equities/holdings_template.csv"),
                           path=None, processed_dir=tmp_path)
    assert loaded.option.code == "B" and {h.ticker for h in loaded.holdings} == {"NVDA", "AAPL", "WALMEX"}


def test_option_a_loads_the_pdf_and_writes_a_review_csv(tmp_path):
    pytest.importorskip("pdfplumber")
    loaded = load_holdings(OPTIONS["A"], BrokerConfig(statement_pdf="tests/fixtures/monitoring/gbm_statement.pdf"),
                           path=None, processed_dir=tmp_path)
    assert loaded.extraction["statement_type"] == "gbm"
    assert Path(loaded.extraction["review_csv"]).exists()


def test_option_c_without_credentials_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IBKR_FLEX_QUERY_ID", raising=False)
    with pytest.raises(IbkrFlexError, match="IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID"):
        load_holdings(OPTIONS["C"], BrokerConfig(), path=None, processed_dir=tmp_path)


# ---------------------------------------------------------------------------
# IBKR Flex: download flow
# ---------------------------------------------------------------------------

def test_flex_flow_polls_until_the_statement_is_ready():
    http = _Http(SEND_OK, IN_PROGRESS, IN_PROGRESS, STATEMENT)
    waits = []
    xml = fetch_statement_xml(CREDS, poll_seconds=5, http_get=http, sleep=waits.append)
    assert b"FlexQueryResponse" in xml and waits == [5, 5]
    send_url, send_params, headers = http.calls[0]
    assert send_url.endswith("/SendRequest") and send_params == {"t": TOKEN, "q": QUERY, "v": "3"}
    assert headers.get("User-Agent")                                  # IBKR rejects requests without one
    poll_url, poll_params, _ = http.calls[1]
    assert "gdcdyn.interactivebrokers.com" in poll_url and poll_params["q"] == "5555"


def test_flex_gives_up_after_the_wait_budget():
    http = _Http(SEND_OK, *([IN_PROGRESS] * 10))
    with pytest.raises(IbkrFlexError, match="not ready"):
        fetch_statement_xml(CREDS, poll_seconds=5, max_wait_seconds=12, http_get=http, sleep=lambda s: None)


def test_flex_error_codes_are_explained():
    with pytest.raises(IbkrFlexError, match=r"1012.*expired.*Client Portal"):
        fetch_statement_xml(CREDS, http_get=_Http(EXPIRED), sleep=lambda s: None)


def test_flex_refuses_to_send_the_token_outside_ibkr():
    evil = SEND_OK.replace(b"https://gdcdyn.interactivebrokers.com", b"https://example.com")
    http = _Http(evil)
    with pytest.raises(IbkrFlexError, match="non-IBKR"):
        fetch_statement_xml(CREDS, http_get=http, sleep=lambda s: None)
    assert len(http.calls) == 1                                       # nothing sent to example.com


@pytest.mark.parametrize("url", ["http://ndcdyn.interactivebrokers.com/x",
                                 "https://interactivebrokers.com.evil.net/x"])
def test_host_check_requires_https_and_the_real_domain(url):
    from asset_selection.monitoring.ibkr_flex import _require_ibkr_host
    with pytest.raises(IbkrFlexError):
        _require_ibkr_host(url)


def test_the_token_never_appears_in_errors_or_logs(caplog):
    http = _Http(_Resp(b"", status=500))
    with caplog.at_level("DEBUG"):
        with pytest.raises(IbkrFlexError) as exc:
            fetch_statement_xml(CREDS, http_get=http, sleep=lambda s: None)
    assert TOKEN not in str(exc.value) and "t=***" in str(exc.value)
    assert TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# IBKR Flex: parsing
# ---------------------------------------------------------------------------

def test_open_positions_become_lots():
    lots, report = parse_open_positions(STATEMENT)
    nvda = [l for l in lots if l.ticker == "NVDA"]
    # Lot rows win over the summary row: 6 + 4 shares, not 10 more on top.
    assert sorted(l.shares for l in nvda) == [4.0, 6.0]
    assert {l.purchase_date for l in nvda} == {date(2025, 3, 14), date(2025, 9, 2)}
    brk = next(l for l in lots if l.ticker == "BRK.B")
    assert brk.unit_cost == pytest.approx(450.0) and brk.derived_fields == ["unit_cost"]
    walmex = next(l for l in lots if l.ticker == "WALMEX")
    assert walmex.market == "BMV" and walmex.currency == "MXN"
    assert all(l.broker == "IBKR" for l in lots)
    assert report.dates_attached == 2 and report.report_date == "20261014"


def test_options_shorts_and_other_currencies_are_skipped_not_guessed():
    lots, report = parse_open_positions(STATEMENT)
    assert {l.ticker for l in lots} == {"NVDA", "BRK.B", "WALMEX"}
    text = " ".join(report.skipped)
    assert "OPT" in text and "short" in text and "EUR" in text
    assert report.confidence == "medium"


def test_summary_only_queries_say_dates_are_unknown():
    summary_only = STATEMENT.replace(b'levelOfDetail="LOT"', b'levelOfDetail="SUMMARY"')
    lots, report = parse_open_positions(summary_only)
    assert all(l.purchase_date is None for l in lots)
    assert any("Lot' level of detail" in w for w in report.warnings)


def test_statement_without_open_positions_is_refused():
    with pytest.raises(IbkrFlexError, match="Open Positions"):
        parse_open_positions(b"<FlexQueryResponse><FlexStatements/></FlexQueryResponse>")


def test_option_c_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("IBKR_FLEX_TOKEN", TOKEN)
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", QUERY)
    import requests
    monkeypatch.setattr(requests, "get", _Http(SEND_OK, STATEMENT))
    loaded = load_holdings(OPTIONS["C"], BrokerConfig(), path=None, processed_dir=tmp_path)
    assert {h.ticker for h in loaded.holdings} == {"NVDA", "BRK.B", "WALMEX"}
    review = Path(loaded.extraction["review_csv"]).read_text()
    assert "NVDA" in review and "U0000000" not in review and TOKEN not in review   # no account id, no token


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_lists_brokers(capsys):
    from asset_selection.pipelines import run_portfolio_monitor as mon
    assert mon.main(["--list-brokers", "--log-level", "ERROR"]) == 0
    out = capsys.readouterr().out
    assert "-> B" in out and " A " in out and " C " in out and "ready" in out


def test_cli_broker_flag_overrides_the_config(capsys):
    from asset_selection.pipelines import run_portfolio_monitor as mon
    assert mon.main(["--list-brokers", "--broker", "C", "--log-level", "ERROR"]) == 0
    assert "-> C" in capsys.readouterr().out


def test_cli_init_holdings(tmp_path, capsys):
    import yaml
    from asset_selection.pipelines import run_portfolio_monitor as mon
    cfg = tmp_path / "m.yaml"
    target = tmp_path / "data" / "my_holdings.csv"
    cfg.write_text(yaml.safe_dump({"broker": {"holdings_csv": str(target)}}))
    assert mon.main(["--config", str(cfg), "--init-holdings", "--log-level", "ERROR"]) == 0
    assert target.exists() and "Created" in capsys.readouterr().out
    assert mon.main(["--config", str(cfg), "--init-holdings", "--log-level", "ERROR"]) == 2


def test_cli_option_c_cannot_run_offline(capsys):
    from asset_selection.pipelines import run_portfolio_monitor as mon
    assert mon.main(["--broker", "C", "--offline", "--log-level", "ERROR"]) == 2
    assert "offline" in capsys.readouterr().err


def test_missing_configured_holdings_file_points_to_init(tmp_path):
    cfg = BrokerConfig(holdings_csv=str(tmp_path / "nope.csv"))
    with pytest.raises(BrokerConfigError, match="--init-holdings"):
        load_holdings(OPTIONS["B"], cfg, path=None, processed_dir=tmp_path)


def test_the_personal_holdings_file_is_really_git_ignored():
    """Checking the path starts with data/ was not enough: holdings/ was not ignored."""
    import shutil
    import subprocess

    if shutil.which("git") is None or not Path(".git").exists():
        pytest.skip("needs a git checkout")
    cfg = load_config("configs/us_equities/monitoring.yaml")
    for path in (cfg.broker.holdings_csv, "data/us_equities/holdings/my_holdings.backup-20261015.csv"):
        res = subprocess.run(["git", "check-ignore", "-q", path], capture_output=True)
        assert res.returncode == 0, f"{path} is not git-ignored"
