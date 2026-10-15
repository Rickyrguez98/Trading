"""Trading app: one local interface over the whole US-equities workflow.

Layers, from the inside out:

* :mod:`trading_app.services` -- plain Python, no web framework. It builds the
  exact CLI commands the pipelines already expose (selection, allocation,
  monitor), runs them as background jobs, and reads their output files. Any
  future front end (another web UI, a mobile client) reuses this layer.
* :mod:`trading_app.web` -- a FastAPI application: a JSON API under ``/api``
  and a server-rendered dashboard. Optional extra ``app`` (Python >= 3.10).
* :mod:`trading_app.cli` -- ``trading-app``, which serves it on 127.0.0.1.

Research output only: the app never places orders.
"""

__all__ = ["services"]
