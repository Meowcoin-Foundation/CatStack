#!/usr/bin/env python3
"""
MFarm Worker Agent - Deployed to each mining rig.
Handles stats collection, miner process management, watchdog, and OC application.
Zero external dependencies - stdlib only.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

VERSION = "0.1.0"

# Paths
CONFIG_PATH = Path("/etc/mfarm/config.json")
STATS_PATH = Path("/var/run/mfarm/stats.json")
COMMAND_PATH = Path("/var/run/mfarm/command")
HWINFO_PATH = Path("/var/run/mfarm/hwinfo.json")
MINER_LOG_PATH = Path("/var/log/mfarm/miner.log")
AGENT_LOG_PATH = Path("/var/log/mfarm/agent.log")
PID_PATH = Path("/var/run/mfarm/miner.pid")

# Ensure dirs exist
for d in [STATS_PATH.parent, Path("/var/log/mfarm"), Path("/etc/mfarm")]:
    d.mkdir(parents=True, exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("meowfarm-agent")


class Config:
    def __init__(self):
        self.stats_interval = 5
        self.watchdog_interval = 30
        self.max_gpu_temp = 90
        self.critical_gpu_temp = 95
        self.max_restarts = 5
        self.restart_window = 600
        self.flight_sheet = None
        self.oc_profile = None
        self.miner_paths = {}
        self.api_ports = {
            "ccminer": 4068, "trex": 4067, "lolminer": 44444,
            "cpuminer-opt": 4048, "xmrig": 44445,
        }

    def load(self):
        if not CONFIG_PATH.exists():
            log.warning("No config file at %s", CONFIG_PATH)
            return
        try:
            data = json.loads(CONFIG_PATH.read_text())
            agent = data.get("agent", {})
            self.stats_interval = agent.get("stats_interval", self.stats_interval)
            self.watchdog_interval = agent.get("watchdog_interval", self.watchdog_interval)
            self.max_gpu_temp = agent.get("max_gpu_temp", self.max_gpu_temp)
            self.critical_gpu_temp = agent.get("critical_gpu_temp", self.critical_gpu_temp)
            self.max_restarts = agent.get("max_restarts_per_window", self.max_restarts)
            self.restart_window = agent.get("restart_window_secs", self.restart_window)
            self.flight_sheet = data.get("flight_sheet")
            self.oc_profile = data.get("oc_profile")
            self.miner_paths = data.get("miner_paths", {})
            self.api_ports = {**self.api_ports, **data.get("api_ports", {})}
            log.info("Config loaded: flight_sheet=%s, oc=%s",
                     self.flight_sheet.get("name") if self.flight_sheet else None,
                     self.oc_profile.get("name") if self.oc_profile else None)
        except Exception as e:
            log.error("Failed to load config: %s", e)


# ── GPU Stats ──────────────────────────────────────────────────────────

def get_nvidia_stats() -> list[dict]:
    """Query nvidia-smi for GPU stats."""
    try:
        fields = "index,name,temperature.gpu,fan.speed,power.draw,power.limit," \
                 "clocks.current.graphics,clocks.current.memory," \
                 "memory.used,memory.total,utilization.gpu,pci.bus_id"
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []

        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 12:
                continue
            try:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temp": _int_or_none(parts[2]),
                    "fan": _int_or_none(parts[3]),
                    "power_draw": _float_or_none(parts[4]),
                    "power_limit": _float_or_none(parts[5]),
                    "core_clock": _int_or_none(parts[6]),
                    "mem_clock": _int_or_none(parts[7]),
                    "mem_used": _int_or_none(parts[8]),
                    "mem_total": _int_or_none(parts[9]),
                    "utilization": _int_or_none(parts[10]),
                    "pci_bus": parts[11],
                })
            except (ValueError, IndexError):
                continue
        return gpus
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def get_amd_stats() -> list[dict]:
    """Query AMD GPU stats via sysfs."""
    gpus = []
    cards_dir = Path("/sys/class/drm")
    if not cards_dir.exists():
        return gpus

    for card in sorted(cards_dir.glob("card[0-9]*")):
        device = card / "device"
        if not device.exists():
            continue

        gpu = {"index": int(card.name.replace("card", "")), "name": "AMD GPU"}

        # Temperature
        for hwmon in (device / "hwmon").glob("hwmon*"):
            temp_file = hwmon / "temp1_input"
            if temp_file.exists():
                try:
                    gpu["temp"] = int(temp_file.read_text().strip()) // 1000
                except (ValueError, OSError):
                    pass

            # Fan
            pwm_file = hwmon / "pwm1"
            pwm_max = hwmon / "pwm1_max"
            if pwm_file.exists() and pwm_max.exists():
                try:
                    pwm = int(pwm_file.read_text().strip())
                    mx = int(pwm_max.read_text().strip())
                    gpu["fan"] = round(pwm / mx * 100) if mx > 0 else 0
                except (ValueError, OSError):
                    pass

            # Power
            power_file = hwmon / "power1_average"
            if power_file.exists():
                try:
                    gpu["power_draw"] = int(power_file.read_text().strip()) / 1_000_000
                except (ValueError, OSError):
                    pass
            break

        # Name from product
        name_file = device / "product_name"
        if name_file.exists():
            try:
                gpu["name"] = name_file.read_text().strip()
            except OSError:
                pass

        gpus.append(gpu)
    return gpus


def get_cpu_stats() -> dict:
    """Get CPU model, temp, and usage."""
    info = {"cores": os.cpu_count()}

    # Model name
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    info["model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass

    # Temperature via sensors
    try:
        result = subprocess.run(
            ["sensors", "-j"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for chip_data in data.values():
                if isinstance(chip_data, dict):
                    for key, val in chip_data.items():
                        if "temp" in key.lower() and isinstance(val, dict):
                            for k2, v2 in val.items():
                                if "input" in k2 and isinstance(v2, (int, float)):
                                    info["temp"] = round(v2)
                                    break
                            if "temp" in info:
                                break
                if "temp" in info:
                    break
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # CPU usage (1-second sample)
    try:
        with open("/proc/stat") as f:
            line1 = f.readline()
        time.sleep(0.1)
        with open("/proc/stat") as f:
            line2 = f.readline()

        vals1 = [int(x) for x in line1.split()[1:]]
        vals2 = [int(x) for x in line2.split()[1:]]
        idle1, idle2 = vals1[3], vals2[3]
        total1, total2 = sum(vals1), sum(vals2)
        delta_total = total2 - total1
        delta_idle = idle2 - idle1
        if delta_total > 0:
            info["usage_pct"] = round((1 - delta_idle / delta_total) * 100, 1)
    except (OSError, ValueError, IndexError):
        pass

    # Frequency
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "cpu MHz" in line:
                    info["freq_mhz"] = round(float(line.split(":", 1)[1].strip()))
                    break
    except (OSError, ValueError):
        pass

    return info


def get_system_stats() -> dict:
    """Get system load, memory, disk."""
    info = {}
    try:
        load = os.getloadavg()
        info["load_1m"] = round(load[0], 2)
    except OSError:
        pass

    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
        info["mem_total_mb"] = meminfo.get("MemTotal", 0) // 1024
        info["mem_used_mb"] = (meminfo.get("MemTotal", 0) - meminfo.get("MemAvailable", 0)) // 1024
    except (OSError, ValueError):
        pass

    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        info["disk_used_pct"] = round(used / total * 100, 1) if total > 0 else 0
    except OSError:
        pass

    return info


# ── Miner API Parsers ──────────────────────────────────────────────────

def _ccminer_tcp_query(port: int, command: str) -> str | None:
    """Send a command to ccminer TCP API and return the response."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(("127.0.0.1", port))
            s.sendall((command + "\n").encode())
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\0" in data:
                    break
        return data.decode("utf-8", errors="replace").strip().rstrip("\0")
    except Exception:
        return None


def query_ccminer_api(port: int) -> dict | None:
    """Query ccminer/cpuminer-opt via TCP API (summary + threads)."""
    try:
        # Get summary
        text = _ccminer_tcp_query(port, "summary")
        if not text:
            return None

        kv = {}
        for item in text.split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                kv[k.strip()] = v.strip()

        result = {
            "hashrate": float(kv.get("KHS", 0)) * 1000,
            "accepted": int(kv.get("ACC", 0)),
            "rejected": int(kv.get("REJ", 0)),
            "algo": kv.get("ALGO", ""),
            "uptime_secs": int(kv.get("UPTIME", 0)),
            "difficulty": float(kv.get("DIFF", 0)),
            "hashrate_units": "H/s",
        }

        # Get per-GPU hashrates from threads command
        threads_text = _ccminer_tcp_query(port, "threads")
        if threads_text:
            gpu_stats = []
            for thread_block in threads_text.split("|"):
                tkv = {}
                for item in thread_block.split(";"):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        tkv[k.strip()] = v.strip()
                if "GPU" in tkv:
                    gpu_stats.append({
                        "gpu_index": int(tkv.get("GPU", 0)),
                        "hashrate": float(tkv.get("KHS", 0)) * 1000,
                    })
            if gpu_stats:
                result["gpu_stats"] = gpu_stats

        return result
    except Exception:
        return None


def query_trex_api(port: int) -> dict | None:
    """Query T-Rex via HTTP API."""
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/summary")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read().decode())
        conn.close()

        gpu_stats = []
        for gpu in data.get("gpus", []):
            gpu_stats.append({
                "hashrate": gpu.get("hashrate", 0),
                "temp": gpu.get("temperature", 0),
                "fan": gpu.get("fan_speed", 0),
                "power": gpu.get("power", 0),
            })

        return {
            "hashrate": data.get("hashrate", 0),
            "accepted": data.get("accepted_count", 0),
            "rejected": data.get("rejected_count", 0),
            "algo": data.get("algorithm", ""),
            "uptime_secs": data.get("uptime", 0),
            "hashrate_units": "H/s",
            "gpu_stats": gpu_stats,
        }
    except Exception:
        return None


def query_lolminer_api(port: int) -> dict | None:
    """Query lolMiner via HTTP API."""
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/summary")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read().decode())
        conn.close()

        mining = data.get("Mining", {})
        session = data.get("Session", {})

        gpu_stats = []
        for gpu in data.get("GPUs", []):
            gpu_stats.append({
                "hashrate": gpu.get("Performance", 0),
                "temp": gpu.get("Temp", 0),
                "fan": gpu.get("Fan", 0),
                "power": gpu.get("Power", 0),
            })

        total_hr = sum(g["hashrate"] for g in gpu_stats) if gpu_stats else 0

        return {
            "hashrate": total_hr,
            "accepted": session.get("Accepted", 0),
            "rejected": session.get("Rejected", 0),
            "algo": mining.get("Algorithm", ""),
            "uptime_secs": session.get("Uptime", 0),
            "hashrate_units": "H/s",
            "gpu_stats": gpu_stats,
        }
    except Exception:
        return None


def query_xmrig_api(port: int) -> dict | None:
    """Query XMRig via HTTP API."""
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/1/summary")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read().decode())
        conn.close()

        results = data.get("results", {})
        hashrate = data.get("hashrate", {})
        hr_total = hashrate.get("total", [0])[0] if hashrate.get("total") else 0

        return {
            "hashrate": hr_total,
            "accepted": results.get("shares_good", 0),
            "rejected": results.get("shares_total", 0) - results.get("shares_good", 0),
            "algo": data.get("algo", ""),
            "uptime_secs": data.get("uptime", 0),
            "hashrate_units": "H/s",
        }
    except Exception:
        return None


API_PARSERS = {
    "ccminer_tcp": query_ccminer_api,
    "trex_http": query_trex_api,
    "lolminer_http": query_lolminer_api,
    "xmrig_http": query_xmrig_api,
}

# Miner name to API type mapping
MINER_API_TYPES = {
    "ccminer": "ccminer_tcp",
    "cpuminer-opt": "ccminer_tcp",
    "cpuminer": "ccminer_tcp",
    "trex": "trex_http",
    "t-rex": "trex_http",
    "lolminer": "lolminer_http",
    "xmrig": "xmrig_http",
}


def parse_ccminer_log_hashrates() -> list[dict]:
    """Parse per-GPU hashrates from ccminer's miner.log as a fallback."""
    import re
    try:
        with open(MINER_LOG_PATH) as f:
            lines = f.readlines()
        # Scan last 200 lines for most recent per-GPU hashrate reports
        gpu_hr = {}
        for line in lines[-200:]:
            # Match: GPU #0: NVIDIA GeForce RTX 4070 Ti SUPER, 5294.61 H/s
            m = re.search(r'GPU\s*#?(\d+):\s*.+?,\s*([\d.]+)\s*([KMG]?H/s)', line)
            if m:
                idx = int(m.group(1))
                hr = float(m.group(2))
                unit = m.group(3)
                if unit == "KH/s":
                    hr *= 1000
                elif unit == "MH/s":
                    hr *= 1e6
                elif unit == "GH/s":
                    hr *= 1e9
                gpu_hr[idx] = hr
        return [{"gpu_index": idx, "hashrate": hr} for idx, hr in sorted(gpu_hr.items())]
    except Exception:
        return []


def query_miner_stats(miner_name: str, api_port: int) -> dict | None:
    """Query the running miner for stats."""
    api_type = MINER_API_TYPES.get(miner_name.lower())
    if not api_type:
        return None
    parser = API_PARSERS.get(api_type)
    if not parser:
        return None
    result = parser(api_port)

    # Fallback: if API reports 0 hashrate but miner is running,
    # parse per-GPU hashrates from the miner log
    if result and result.get("hashrate", 0) == 0 and miner_name.lower() in ("ccminer", "cpuminer-opt", "cpuminer"):
        log_gpu = parse_ccminer_log_hashrates()
        if log_gpu:
            result["gpu_stats"] = log_gpu
            result["hashrate"] = sum(g["hashrate"] for g in log_gpu)

    return result


# ── Miner Process Management ──────────────────────────────────────────

class MinerManager:
    def __init__(self, config: Config):
        self.config = config
        self.process: subprocess.Popen | None = None
        self.restart_times: list[float] = []
        self.total_restarts = 0

    @property
    def miner_name(self) -> str | None:
        fs = self.config.flight_sheet
        return fs.get("miner") if fs else None

    @property
    def api_port(self) -> int:
        name = self.miner_name or ""
        return self.config.api_ports.get(name, 4068)

    def build_command(self) -> list[str] | None:
        """Build miner command line from flight sheet."""
        fs = self.config.flight_sheet
        if not fs:
            return None

        miner = fs.get("miner", "")
        algo = fs.get("algo", "")
        pool = fs.get("pool_url", "")
        wallet = fs.get("wallet", "")
        worker = fs.get("worker", socket.gethostname())
        password = fs.get("password", "x")
        extra = fs.get("extra_args", "")
        is_solo = fs.get("is_solo", False)
        port = self.api_port

        # Resolve miner binary path
        binary = self.config.miner_paths.get(miner, miner)

        if miner in ("ccminer", "cpuminer-opt", "cpuminer"):
            cmd = [binary, "-a", algo]
            if is_solo:
                node_url = pool
                rpc_user = fs.get("solo_rpc_user", "")
                rpc_pass = fs.get("solo_rpc_pass", "")
                coinbase = fs.get("coinbase_addr", "")
                cmd += ["-o", node_url, "-u", rpc_user, "-p", rpc_pass,
                        "--no-stratum", f"--coinbase-addr={coinbase}"]
                if "--no-longpoll" not in extra:
                    cmd += ["--no-longpoll"]
            else:
                wallet_worker = f"{wallet}.{worker}"
                cmd += ["-o", pool, "-u", wallet_worker, "-p", password]
            cmd += ["-b", f"0.0.0.0:{port}", "--no-color"]

        elif miner in ("trex", "t-rex"):
            wallet_worker = f"{wallet}.{worker}"
            cmd = [binary, "-a", algo, "-o", pool, "-u", wallet_worker,
                   "-p", password, f"--api-bind-http=0.0.0.0:{port}"]

        elif miner == "lolminer":
            wallet_worker = f"{wallet}.{worker}"
            cmd = [binary, "--algo", algo, "--pool", pool,
                   "--user", wallet_worker, "--pass", password,
                   f"--apiport={port}"]

        elif miner == "xmrig":
            wallet_worker = f"{wallet}.{worker}"
            cmd = [binary, "-a", algo, "-o", pool, "-u", wallet_worker,
                   "-p", password, f"--http-host=0.0.0.0", f"--http-port={port}"]

        else:
            # Generic fallback
            wallet_worker = f"{wallet}.{worker}"
            cmd = [binary, "-a", algo, "-o", pool, "-u", wallet_worker, "-p", password]

        # Append extra args
        if extra:
            cmd += extra.split()

        return cmd

    def apply_overclock(self):
        """Apply OC settings before starting the miner."""
        oc = self.config.oc_profile
        if not oc:
            return

        log.info("Applying OC profile: %s", oc.get("name", "unnamed"))

        per_gpu = oc.get("per_gpu")
        if per_gpu:
            for gpu_oc in per_gpu:
                idx = gpu_oc.get("gpu", 0)
                self._apply_oc_to_gpu(idx, gpu_oc)
        else:
            # Apply global settings to all GPUs
            gpu_count = self._get_gpu_count()
            for idx in range(gpu_count):
                self._apply_oc_to_gpu(idx, oc)

    def _get_gpu_count(self) -> int:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            return len(result.stdout.strip().split("\n")) if result.returncode == 0 else 0
        except Exception:
            return 0

    def _apply_oc_to_gpu(self, idx: int, settings: dict):
        """Apply OC settings to a single GPU."""
        # Persistence mode
        _run_quiet(["nvidia-smi", "-pm", "1"])

        pl = settings.get("power_limit")
        if pl is not None:
            _run_quiet(["nvidia-smi", "-i", str(idx), "-pl", str(pl)])
            log.info("  GPU %d: power limit = %dW", idx, pl)

        core = settings.get("core_offset")
        if core is not None:
            # Try nvidia-settings first (needs X), fall back to lock clocks
            rc = _run_quiet([
                "nvidia-settings", "-a",
                f"[gpu:{idx}]/GPUGraphicsClockOffsetAllPerformanceLevels={core}",
            ])
            if rc != 0:
                log.info("  GPU %d: nvidia-settings unavailable, using lock clocks", idx)
            else:
                log.info("  GPU %d: core offset = %+d MHz", idx, core)

        mem = settings.get("mem_offset")
        if mem is not None:
            rc = _run_quiet([
                "nvidia-settings", "-a",
                f"[gpu:{idx}]/GPUMemoryTransferRateOffsetAllPerformanceLevels={mem}",
            ])
            if rc == 0:
                log.info("  GPU %d: mem offset = %+d MHz", idx, mem)

        fan = settings.get("fan_speed")
        if fan is not None:
            _run_quiet(["nvidia-settings", "-a", f"[gpu:{idx}]/GPUFanControlState=1"])
            _run_quiet(["nvidia-settings", "-a", f"[fan:{idx}]/GPUTargetFanSpeed={fan}"])
            log.info("  GPU %d: fan = %d%%", idx, fan)

    def start(self) -> bool:
        """Start the miner process."""
        if self.is_running():
            log.info("Miner already running (PID %d)", self.process.pid)
            return True

        cmd = self.build_command()
        if not cmd:
            log.warning("No flight sheet configured, cannot start miner")
            return False

        self.apply_overclock()

        log.info("Starting miner: %s", " ".join(cmd))
        try:
            log_file = open(MINER_LOG_PATH, "a")
            self.process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
            PID_PATH.write_text(str(self.process.pid))
            log.info("Miner started with PID %d", self.process.pid)
            return True
        except Exception as e:
            log.error("Failed to start miner: %s", e)
            return False

    def stop(self):
        """Stop the miner process."""
        if self.process and self.process.poll() is None:
            log.info("Stopping miner (PID %d)", self.process.pid)
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                self.process.wait(timeout=5)
            except Exception as e:
                log.error("Error stopping miner: %s", e)
        self.process = None
        if PID_PATH.exists():
            PID_PATH.unlink()

    def restart(self):
        """Restart the miner."""
        self.stop()
        time.sleep(2)
        self.start()

    def is_running(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None

    def get_pid(self) -> int | None:
        if self.process and self.is_running():
            return self.process.pid
        return None

    def check_and_restart(self) -> bool:
        """Watchdog check. Returns True if miner is healthy."""
        if not self.config.flight_sheet:
            return True  # No flight sheet, nothing to watch

        if not self.is_running():
            log.warning("Miner is not running, attempting restart")
            return self._do_restart()

        # Check if miner is producing hashrate
        stats = query_miner_stats(self.miner_name or "", self.api_port)
        if stats and stats.get("hashrate", 0) == 0:
            log.warning("Miner hashrate is 0, may be hung")
            # Give it one more check before restarting
            return True  # Don't restart yet, flag for next check

        return True

    def _do_restart(self) -> bool:
        """Attempt restart with rate limiting."""
        now = time.time()
        # Clean old restart timestamps
        self.restart_times = [t for t in self.restart_times
                              if now - t < self.config.restart_window]

        if len(self.restart_times) >= self.config.max_restarts:
            log.error("Too many restarts (%d in %ds), backing off 60s",
                      len(self.restart_times), self.config.restart_window)
            time.sleep(60)

        self.restart_times.append(now)
        self.total_restarts += 1
        log.info("Restart attempt #%d", self.total_restarts)

        self.stop()
        time.sleep(5)
        return self.start()


# ── Main Agent Loop ───────────────────────────────────────────────────

class Agent:
    def __init__(self):
        self.config = Config()
        self.miner = MinerManager(self.config)
        self.running = True
        self.zero_hashrate_count = 0

    def run(self):
        log.info("MeowFarm Agent v%s starting on %s", VERSION, socket.gethostname())
        self.config.load()

        # Start miner if flight sheet is configured
        if self.config.flight_sheet:
            self.miner.start()

        # Run stats and watchdog on separate timers
        stats_thread = threading.Thread(target=self._stats_loop, daemon=True)
        watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        command_thread = threading.Thread(target=self._command_loop, daemon=True)

        stats_thread.start()
        watchdog_thread.start()
        command_thread.start()

        # Main thread waits for signal
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

        while self.running:
            time.sleep(1)

        log.info("Agent shutting down")
        self.miner.stop()

    def _shutdown(self, signum, frame):
        log.info("Received signal %d, shutting down", signum)
        self.running = False

    def _stats_loop(self):
        while self.running:
            try:
                self._collect_and_write_stats()
            except Exception as e:
                log.error("Stats collection error: %s", e)
            time.sleep(self.config.stats_interval)

    def _watchdog_loop(self):
        while self.running:
            try:
                self._watchdog_check()
            except Exception as e:
                log.error("Watchdog error: %s", e)
            time.sleep(self.config.watchdog_interval)

    def _command_loop(self):
        """Watch for command file from console."""
        while self.running:
            try:
                if COMMAND_PATH.exists():
                    cmd = COMMAND_PATH.read_text().strip()
                    COMMAND_PATH.unlink()
                    if cmd:
                        self._handle_command(cmd)
            except Exception as e:
                log.error("Command handler error: %s", e)
            time.sleep(2)

    def _handle_command(self, cmd: str):
        log.info("Received command: %s", cmd)
        if cmd == "restart_miner":
            self.miner.restart()
        elif cmd == "stop_miner":
            self.miner.stop()
        elif cmd == "start_miner":
            self.miner.start()
        elif cmd == "apply_config":
            self.config.load()
            self.miner.stop()
            time.sleep(2)
            if self.config.flight_sheet:
                self.miner.start()
        elif cmd == "update_agent":
            log.info("Agent update requested, restarting via systemd...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            log.warning("Unknown command: %s", cmd)

    def _collect_and_write_stats(self):
        """Collect all stats and write to stats.json."""
        hostname = socket.gethostname()

        # Get uptime
        try:
            with open("/proc/uptime") as f:
                uptime_secs = int(float(f.readline().split()[0]))
        except (OSError, ValueError):
            uptime_secs = 0

        # GPU stats
        gpus = get_nvidia_stats()
        if not gpus:
            gpus = get_amd_stats()

        # Miner stats
        miner_stats = {}
        if self.config.flight_sheet and self.miner.is_running():
            fs = self.config.flight_sheet
            api_stats = query_miner_stats(fs.get("miner", ""), self.miner.api_port)
            miner_stats = {
                "name": fs.get("miner", ""),
                "version": fs.get("miner_version", ""),
                "algo": fs.get("algo", ""),
                "pool": fs.get("pool_url", ""),
                "pid": self.miner.get_pid(),
                "running": self.miner.is_running(),
                "restarts": self.miner.total_restarts,
            }
            if api_stats:
                miner_stats.update(api_stats)

                # Merge per-GPU hashrate from miner API into GPU stats
                gpu_api = api_stats.get("gpu_stats", [])
                for i, gs in enumerate(gpu_api):
                    if i < len(gpus):
                        gpus[i]["hashrate"] = gs.get("hashrate", 0)
        elif self.config.flight_sheet:
            fs = self.config.flight_sheet
            miner_stats = {
                "name": fs.get("miner", ""),
                "algo": fs.get("algo", ""),
                "running": False,
            }

        stats = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_version": VERSION,
            "hostname": hostname,
            "uptime_secs": uptime_secs,
            "miner": miner_stats,
            "gpus": gpus,
            "cpu": get_cpu_stats(),
            "system": get_system_stats(),
        }

        # Atomic write
        tmp = STATS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(stats, indent=2))
        tmp.rename(STATS_PATH)

    def _watchdog_check(self):
        """Check miner health and GPU temps."""
        if not self.config.flight_sheet:
            return

        # Check GPU temps
        gpus = get_nvidia_stats() or get_amd_stats()
        for gpu in gpus:
            temp = gpu.get("temp")
            if temp is None:
                continue
            if temp >= self.config.critical_gpu_temp:
                log.critical("GPU %d temp %d°C >= critical %d°C, STOPPING MINER",
                             gpu.get("index", 0), temp, self.config.critical_gpu_temp)
                self.miner.stop()
                return
            if temp >= self.config.max_gpu_temp:
                log.warning("GPU %d temp %d°C >= warning %d°C",
                            gpu.get("index", 0), temp, self.config.max_gpu_temp)

        # Check miner is running
        if not self.miner.is_running():
            log.warning("Watchdog: miner not running")
            self.miner._do_restart()
            self.zero_hashrate_count = 0
            return

        # Check hashrate
        stats = query_miner_stats(self.miner.miner_name or "", self.miner.api_port)
        if stats and stats.get("hashrate", 0) == 0:
            self.zero_hashrate_count += 1
            log.warning("Watchdog: zero hashrate (count: %d/3)", self.zero_hashrate_count)
            if self.zero_hashrate_count >= 3:
                log.warning("Watchdog: 3 consecutive zero hashrate checks, restarting miner")
                self.miner._do_restart()
                self.zero_hashrate_count = 0
        else:
            self.zero_hashrate_count = 0


# ── Helpers ───────────────────────────────────────────────────────────

def _int_or_none(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _float_or_none(s: str) -> float | None:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _run_quiet(cmd: list[str]) -> int:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        return result.returncode
    except Exception:
        return -1


if __name__ == "__main__":
    agent = Agent()
    agent.run()
