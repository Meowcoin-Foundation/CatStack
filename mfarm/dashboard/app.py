"""Main dashboard application using Rich Live display."""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from mfarm.config import DASHBOARD_REFRESH_INTERVAL, SSH_MAX_WORKERS
from mfarm.db.connection import get_db
from mfarm.db.models import Rig
from mfarm.dashboard.rig_table import build_rig_table
from mfarm.dashboard.rig_detail import build_rig_detail
from mfarm.ssh.pool import get_pool

console = Console()


class Dashboard:
    def __init__(self, group_filter: str | None = None, refresh_interval: int = DASHBOARD_REFRESH_INTERVAL):
        self.group_filter = group_filter
        self.refresh_interval = refresh_interval
        self.rig_stats: dict[str, tuple[dict | None, Exception | None]] = {}
        self.rig_info: dict[str, dict] = {}  # DB info per rig name
        self.rigs: list[Rig] = []
        self.mode = "table"  # "table" or "detail"
        self.detail_rig: str | None = None
        self.running = True
        self._input_buffer = ""

    def load_rigs(self):
        db = get_db()
        self.rigs = Rig.get_all(db, group_name=self.group_filter)
        for rig in self.rigs:
            self.rig_info[rig.name] = {
                "host": rig.host,
                "flight_sheet_name": rig.flight_sheet_name,
                "oc_profile_name": rig.oc_profile_name,
                "group_name": rig.group_name,
            }

    def poll_all(self):
        pool = get_pool()
        with ThreadPoolExecutor(max_workers=SSH_MAX_WORKERS) as executor:
            futures = {}
            for rig in self.rigs:
                futures[executor.submit(self._poll_one, pool, rig)] = rig.name

            for future in as_completed(futures, timeout=15):
                name = futures[future]
                try:
                    future.result()  # results stored via _poll_one
                except Exception as e:
                    self.rig_stats[name] = (None, e)

    def _poll_one(self, pool, rig: Rig):
        try:
            stdout, _, rc = pool.exec(rig, "cat /var/run/mfarm/stats.json", timeout=5)
            if rc == 0 and stdout.strip():
                stats = json.loads(stdout)
                self.rig_stats[rig.name] = (stats, None)
            else:
                self.rig_stats[rig.name] = (None, None)
        except Exception as e:
            self.rig_stats[rig.name] = (None, e)

    def render(self):
        if self.mode == "detail" and self.detail_rig:
            stats, err = self.rig_stats.get(self.detail_rig, (None, None))
            info = self.rig_info.get(self.detail_rig)
            return build_rig_detail(self.detail_rig, stats, info)
        else:
            title = "MeowFarm Dashboard"
            if self.group_filter:
                title += f" [group:{self.group_filter}]"
            return build_rig_table(self.rig_stats, title=title)

    def handle_key(self, key: str):
        if key.lower() == "q":
            self.running = False
        elif key.lower() == "r":
            pass  # Will refresh on next cycle
        elif key.lower() == "b" and self.mode == "detail":
            self.mode = "table"
            self.detail_rig = None
        elif key.lower() == "d" and self.mode == "table":
            # Enter detail mode - select rig by number
            self._input_buffer = "d"
        elif key.isdigit() and self._input_buffer == "d":
            idx = int(key)
            rig_names = sorted(self.rig_stats.keys())
            if 0 <= idx < len(rig_names):
                self.detail_rig = rig_names[idx]
                self.mode = "detail"
            self._input_buffer = ""
        elif key.lower() == "g":
            # Cycle group filter would go here
            pass
        else:
            self._input_buffer = ""

    def run(self):
        self.load_rigs()

        if not self.rigs:
            console.print("[dim]No rigs configured. Add rigs first with: mfarm rig add <name> <host>[/dim]")
            return

        console.print(f"[bold]Starting dashboard with {len(self.rigs)} rig(s)...[/bold]")
        console.print("[dim]Press Q to quit, R to refresh, D+<num> for detail, B to go back[/dim]\n")

        # Initial poll
        self.poll_all()

        # Set up keyboard input thread (non-blocking)
        if sys.platform == "win32":
            input_thread = threading.Thread(target=self._windows_input, daemon=True)
        else:
            input_thread = threading.Thread(target=self._unix_input, daemon=True)
        input_thread.start()

        try:
            with Live(self.render(), console=console, refresh_per_second=1) as live:
                last_poll = time.time()
                while self.running:
                    # Re-poll on interval
                    now = time.time()
                    if now - last_poll >= self.refresh_interval:
                        self.poll_all()
                        last_poll = now

                    live.update(self.render())
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False

    def _windows_input(self):
        """Non-blocking keyboard input on Windows."""
        try:
            import msvcrt
            while self.running:
                if msvcrt.kbhit():
                    key = msvcrt.getch().decode("utf-8", errors="ignore")
                    self.handle_key(key)
                time.sleep(0.1)
        except Exception:
            pass

    def _unix_input(self):
        """Non-blocking keyboard input on Unix."""
        try:
            import tty
            import termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while self.running:
                    key = sys.stdin.read(1)
                    if key:
                        self.handle_key(key)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass
