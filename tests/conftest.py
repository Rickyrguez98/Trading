"""Make ``src`` importable without an editable install."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


import pytest  # noqa: E402


_SECRETS = ("NEWSAPI_AI_KEY", "NEWSAPI_AI_MAX_CALLS", "FINNHUB_API_KEY", "IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID",
            "ALPHAVANTAGE_API_KEY", "FMP_API_KEY")


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Tests never see your real keys.

    The pipeline modules call ``load_dotenv()`` on import, which would put the
    keys from your ``.env`` into this process; a test that builds the shipped
    news chain would then spend real API tokens. Tests that need a key set a
    fake one themselves.
    """
    for name in _SECRETS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _throttle_state(monkeypatch):
    """Fresh rate-limit state for every test, and no real sleeping.

    Throttles are shared per provider name (by design: one pause for every
    caller), so a test that trips one must not leak a paused provider into the
    next. The recorded pauses are available to tests that ask for this fixture.
    """
    from trading_common import throttle

    throttle.reset_throttles()
    slept = []
    monkeypatch.setattr(throttle, "_sleep", slept.append)
    yield slept
    throttle.reset_throttles()
