"""
MeowFarm Desktop Launcher
Double-click to start the web dashboard. Auto-restarts on crash.
"""
import os
import sys
import time
import subprocess
import threading
import webbrowser

PORT = 8888
LOG_FILE = os.path.join(os.path.expanduser("~"), ".mfarm", "meowfarm-web.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

browser_opened = False


def run_server():
    """Run uvicorn as a subprocess so imports are clean."""
    global browser_opened

    while True:
        with open(LOG_FILE, "a") as log:
            log.write(f"\n=== MeowFarm starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log.flush()

        # Launch uvicorn as a subprocess from the user's home dir
        # This avoids the C:\Source import conflict entirely
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "mfarm.web.app:app",
             "--host", "0.0.0.0", "--port", str(PORT), "--log-level", "info"],
            cwd=os.path.expanduser("~"),
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
        )

        # Open browser once on first successful start
        if not browser_opened:
            def open_browser():
                time.sleep(3)
                webbrowser.open(f"http://localhost:{PORT}")
            threading.Thread(target=open_browser, daemon=True).start()
            browser_opened = True

        # Wait for process to exit
        proc.wait()

        with open(LOG_FILE, "a") as log:
            log.write(f"\n!!! Server exited with code {proc.returncode}. Restarting in 5s...\n")

        time.sleep(5)


if __name__ == "__main__":
    run_server()
