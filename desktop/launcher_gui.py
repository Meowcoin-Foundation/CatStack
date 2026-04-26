"""
CatStack GUI entry point.

Boots the FastAPI dashboard in a background thread, then opens a chromeless
Edge/Chrome window pointed at it (``--app=`` mode). From the user's view it's
a native app: own taskbar icon, no browser chrome, no address bar. When the
window is closed the process exits.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path


DEFAULT_PORT = 8888


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_port() -> int:
    raw = os.environ.get("CATSTACK_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    # Prefer the well-known default so bookmarks keep working, but fall back
    # to an ephemeral free port if something else is already on 8888
    # (e.g. the user's dev catstack.pyw).
    if _port_free(DEFAULT_PORT):
        return DEFAULT_PORT
    return _find_free_port()


def _log_path() -> Path:
    home = Path(os.environ.get("MFARM_HOME", Path.home() / ".mfarm"))
    home.mkdir(parents=True, exist_ok=True)
    return home / "catstack-gui.log"


def _find_app_browser() -> str | None:
    """Locate a Chromium-based browser that supports ``--app=``."""
    candidates = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            rf"{pfx86}\Microsoft\Edge\Application\msedge.exe",
            rf"{pf}\Microsoft\Edge\Application\msedge.exe",
            rf"{pf}\Google\Chrome\Application\chrome.exe",
            rf"{pfx86}\Google\Chrome\Application\chrome.exe",
            rf"{local}\Google\Chrome\Application\chrome.exe",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/snap/bin/chromium",
        ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except (ConnectionRefusedError, socket.timeout, OSError):
                time.sleep(0.2)
    return False


def _run_server(port: int, log_file: Path) -> None:
    import uvicorn
    from mfarm.web.app import app

    with open(log_file, "a", encoding="utf-8", buffering=1) as fh:
        sys.stdout = fh
        sys.stderr = fh
        try:
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
        except Exception as exc:
            fh.write(f"\n!!! uvicorn crashed: {exc!r}\n")


def main() -> int:
    port = _resolve_port()
    log_file = _log_path()

    server_thread = threading.Thread(
        target=_run_server, args=(port, log_file), daemon=True
    )
    server_thread.start()

    if not _wait_for_port(port):
        # Fall back to opening in whatever default browser exists; at least
        # the user sees *something* instead of silent failure.
        webbrowser.open(f"http://127.0.0.1:{port}")
        time.sleep(60)
        return 1

    url = f"http://127.0.0.1:{port}"
    browser = _find_app_browser()
    if browser is None:
        webbrowser.open(url)
        # No way to know when the user closes a regular browser tab — just
        # keep the server alive until killed.
        server_thread.join()
        return 0

    # Per-PID profile dir so concurrent/rapid re-launches can't fight for
    # the same Chromium SingletonLock. Old profiles are orphaned but get
    # garbage-collected next run (see below).
    profile_dir = os.path.join(
        tempfile.gettempdir(), f"catstack-app-profile-{os.getpid()}"
    )
    os.makedirs(profile_dir, exist_ok=True)
    _sweep_old_profile_dirs(keep=profile_dir)
    url_marker = f"--app={url}"
    subprocess.Popen(
        [
            browser,
            url_marker,
            f"--user-data-dir={profile_dir}",
            "--window-size=1400,900",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        **_no_window_kwargs(),
    )
    # The launcher exe for Chromium browsers often exits immediately after
    # signaling an existing browser session, so we can't use proc.wait() to
    # detect window close. Instead: wait for the app window to actually
    # appear (cold-start Edge can take 5-10s), then poll for it to disappear.
    # Tolerate short transient misses — Edge restructures its process tree
    # during navigation, briefly losing the --app=URL marker.
    appeared = False
    for _ in range(60):                  # up to 30s for the window to appear
        if _app_window_running(url_marker):
            appeared = True
            break
        time.sleep(0.5)

    if not appeared:
        # Edge never showed up with our URL. Best we can do is keep the server
        # running so whatever browser the user does have can reach it.
        webbrowser.open(url)
        server_thread.join()
        return 0

    misses = 0
    while True:
        time.sleep(2)
        if _app_window_running(url_marker):
            misses = 0
        else:
            misses += 1
            if misses >= 3:              # 6s without the window -> exit
                return 0


def _sweep_old_profile_dirs(keep: str) -> None:
    """Remove catstack-app-profile-* dirs from previous launches, best-effort.

    Directories still in use by a running browser will fail to delete; we
    just skip them.
    """
    import shutil
    tmp = tempfile.gettempdir()
    try:
        for name in os.listdir(tmp):
            if not name.startswith("catstack-app-profile-"):
                continue
            path = os.path.join(tmp, name)
            if os.path.abspath(path) == os.path.abspath(keep):
                continue
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _no_window_kwargs() -> dict:
    """subprocess kwargs that suppress the brief CMD window flash on Windows."""
    if sys.platform != "win32":
        return {}
    # CREATE_NO_WINDOW = 0x08000000. Defined this way so the file imports
    # cleanly on Linux (where subprocess.CREATE_NO_WINDOW does not exist).
    return {"creationflags": 0x08000000}


def _app_window_running(url_marker: str) -> bool:
    if sys.platform == "win32":
        try:
            ps = (
                "Get-CimInstance Win32_Process "
                "-Filter \"Name='msedge.exe' OR Name='chrome.exe' OR Name='chromium.exe'\" "
                "| Where-Object { $_.CommandLine -like '*" + url_marker + "*' } "
                "| Measure-Object | Select-Object -ExpandProperty Count"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps],
                stderr=subprocess.DEVNULL, timeout=5,
                **_no_window_kwargs(),
            ).decode("utf-8", errors="ignore").strip()
            return out.isdigit() and int(out) > 0
        except Exception:
            return True
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "args"], stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", errors="ignore")
        return url_marker in out
    except Exception:
        return True


if __name__ == "__main__":
    sys.exit(main())
