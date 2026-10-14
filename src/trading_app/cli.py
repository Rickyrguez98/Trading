"""``trading-app``: serve the local web app on 127.0.0.1 and open it.

    pip install -e ".[app]"        # Python >= 3.10
    trading-app                    # from the Trading project folder
"""
from __future__ import annotations

import argparse
import ipaddress
import sys
import threading
import webbrowser
from pathlib import Path
from typing import List, Optional


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trading-app",
                                description="Local web app over selection, allocation, broker options and the monitor.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1",
                   help="Loopback only (127.0.0.1 / ::1): the app has no login and must not be exposed.")
    p.add_argument("--root", default=".", help="The Trading project folder (default: current directory).")
    p.add_argument("--no-browser", action="store_true", help="Do not open a browser tab.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if not ipaddress.ip_address(args.host).is_loopback:
            raise ValueError
    except ValueError:
        print("Refusing to listen on a non-loopback address: the app has no login.", file=sys.stderr)
        return 2
    root = Path(args.root).resolve()
    if not (root / "configs" / "us_equities").is_dir():
        print(f"{root} does not look like the Trading project (no configs/us_equities). "
              "Run trading-app from the project folder or pass --root.", file=sys.stderr)
        return 2
    try:
        import uvicorn

        from .services import Workflow, default_paths
        from .web.app import create_app
    except ImportError as exc:
        print(f"The app needs its optional dependencies ({exc.name} is missing). Install them with:\n"
              "    pip install -e \".[app]\"\n(Python 3.10 or newer is required.)", file=sys.stderr)
        return 2
    try:  # keys in .env (FINNHUB_API_KEY, IBKR_FLEX_TOKEN) reach the jobs through the environment
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
    except ImportError:  # pragma: no cover - python-dotenv is a core dependency
        pass

    app = create_app(Workflow(default_paths(root)))
    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Trading app on {url} (Ctrl-C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
