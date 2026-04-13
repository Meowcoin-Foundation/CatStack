#!/usr/bin/env python3
"""
MeowOS Web UI - Standalone rig setup wizard and monitoring dashboard.
Runs on each rig at http://<rig-ip>:8888

Zero dependencies beyond Python 3 stdlib.
Communicates with mfarm-agent via:
  - /var/run/mfarm/stats.json (read stats)
  - /var/run/mfarm/hwinfo.json (read hardware info)
  - /etc/mfarm/config.json (read/write config)
  - /var/run/mfarm/command (write commands for agent)
"""
import json
import os
import socket
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = 8888
CONFIG_PATH = "/etc/mfarm/config.json"
STATS_PATH = "/var/run/mfarm/stats.json"
HWINFO_PATH = "/var/run/mfarm/hwinfo.json"
COMMAND_PATH = "/var/run/mfarm/command"
MINER_LOG = "/var/log/mfarm/miner.log"
HTML_PATH = "/opt/mfarm/meowos-webui.html"

# Embedded miner registry (no imports from mfarm package)
MINERS = [
    {"name": "ccminer", "display": "CCMiner", "gpu": "nvidia", "solo": True,
     "algos": ["yescrypt","yescryptR8","yescryptR16","yescryptR32","scrypt","sha256d","sha256t","keccak","lyra2v2","lyra2v3","lyra2z","neoscrypt","x11","x13","x16r","x16s","x17","qubit","quark","blake2s","skein","groestl","myr-gr","allium","phi2","tribus"]},
    {"name": "cpuminer-opt", "display": "CPUMiner-Opt", "gpu": "cpu", "solo": True,
     "algos": ["yescrypt","yescryptR8","yescryptR16","yescryptR32","scrypt","sha256d","x11","x16r","x17","lyra2v2","lyra2v3","ghostrider","minotaur","minotaurx","randomx"]},
    {"name": "trex", "display": "T-Rex Miner", "gpu": "nvidia", "solo": False,
     "algos": ["ethash","etchash","kawpow","octopus","autolykos2","firopow","blake3","sha256t"]},
    {"name": "lolminer", "display": "lolMiner", "gpu": "any", "solo": False,
     "algos": ["ethash","etchash","autolykos2","beamhashiii","equihash","ton"]},
    {"name": "xmrig", "display": "XMRig", "gpu": "any", "solo": False,
     "algos": ["randomx","rx/0","rx/wow","kawpow","ghostrider","cn/r","argon2/chukwa"]},
    {"name": "miniz", "display": "miniZ", "gpu": "nvidia", "solo": False,
     "algos": ["equihash144_5","equihash192_7","beamhashiii","ethash","etchash","progpow","octopus"]},
]


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def send_command(cmd):
    Path(COMMAND_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(COMMAND_PATH, "w") as f:
        f.write(cmd)


def get_rig_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def tail_log(path, lines=50):
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), path],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout
    except Exception:
        return ""


class MeowOSHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # Silence request logging

    def respond_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def respond_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            try:
                with open(HTML_PATH) as f:
                    self.respond_html(f.read())
            except FileNotFoundError:
                self.respond_html("<h1>MeowOS</h1><p>Web UI HTML not found</p>")

        elif self.path == "/api/status":
            stats = read_json(STATS_PATH)
            hwinfo = read_json(HWINFO_PATH)
            config = read_json(CONFIG_PATH)
            self.respond_json({
                "stats": stats,
                "hwinfo": hwinfo,
                "hostname": socket.gethostname(),
                "ip": get_rig_ip(),
                "uptime": self._get_uptime(),
                "configured": config.get("flight_sheet") is not None,
            })

        elif self.path == "/api/config":
            self.respond_json(read_json(CONFIG_PATH))

        elif self.path == "/api/miners":
            self.respond_json(MINERS)

        elif self.path == "/api/miner-log":
            self.respond_json({"log": tail_log(MINER_LOG, 100)})

        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/config":
            body = self.read_body()
            config = read_json(CONFIG_PATH)

            if "flight_sheet" in body:
                config["flight_sheet"] = body["flight_sheet"]
            if "oc_profile" in body:
                config["oc_profile"] = body["oc_profile"]

            write_json(CONFIG_PATH, config)
            send_command("apply_config")
            self.respond_json({"ok": True})

        elif self.path == "/api/restart-miner":
            send_command("restart_miner")
            self.respond_json({"ok": True})

        elif self.path == "/api/stop-miner":
            send_command("stop_miner")
            self.respond_json({"ok": True})

        elif self.path == "/api/reboot":
            self.respond_json({"ok": True})
            subprocess.Popen(["reboot"], stdout=subprocess.DEVNULL)

        elif self.path == "/api/change-password":
            body = self.read_body()
            pw = body.get("password", "")
            if pw:
                subprocess.run(
                    ["chpasswd"],
                    input=f"miner:{pw}\n", text=True, timeout=5
                )
                self.respond_json({"ok": True})
            else:
                self.respond_json({"error": "No password"}, 400)

        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _get_uptime(self):
        try:
            with open("/proc/uptime") as f:
                return float(f.read().split()[0])
        except Exception:
            return 0


def main():
    os.makedirs("/var/run/mfarm", exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), MeowOSHandler)
    ip = get_rig_ip()
    print(f"MeowOS Web UI running at http://{ip}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
