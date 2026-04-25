"""
CatStack native app entry point.

When frozen with PyInstaller this becomes the single ``CatStack`` executable.

Behavior:
- No CLI args  -> start the web dashboard, open the browser, restart on crash.
- Any CLI args -> dispatch to the ``mfarm`` click CLI (so the same binary
  doubles as the CLI tool: ``CatStack rig list``, ``CatStack status``, etc.).
"""
from __future__ import annotations

import os
import sys
import time
import threading
import webbrowser
from pathlib import Path


DEFAULT_PORT = 8888


def _resolve_port() -> int:
    raw = os.environ.get("CATSTACK_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        print(f"Ignoring invalid CATSTACK_PORT={raw!r}, using {DEFAULT_PORT}")
        return DEFAULT_PORT


def _log_path() -> Path:
    home = Path(os.environ.get("MFARM_HOME", Path.home() / ".mfarm"))
    home.mkdir(parents=True, exist_ok=True)
    return home / "catstack-web.log"


def _open_browser_when_ready(url: str, delay: float = 2.0) -> None:
    def _opener():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_opener, daemon=True).start()


def _run_dashboard() -> None:
    """Run the FastAPI dashboard in-process via uvicorn, restarting on crash."""
    import uvicorn

    port = _resolve_port()
    log_file = _log_path()
    url = f"http://localhost:{port}"
    print(f"CatStack dashboard starting at {url}")
    print(f"Logs: {log_file}")
    _open_browser_when_ready(url)

    while True:
        try:
            from mfarm.web.app import app
            uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
        except KeyboardInterrupt:
            print("\nCatStack stopped.")
            return
        except Exception as exc:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(f"\n!!! Server crashed: {exc!r}. Restarting in 5s...\n")
            time.sleep(5)


def _run_cli() -> None:
    from mfarm.cli import cli
    cli()


def main() -> None:
    if len(sys.argv) > 1:
        _run_cli()
    else:
        _run_dashboard()


if __name__ == "__main__":
    main()
