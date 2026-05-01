#!/usr/bin/env python3
"""
MeowOS Phone-Home Service
Broadcasts this rig's IP, MAC, and hostname to the MeowFarm server every 60 seconds.
Runs as a systemd service so the rig is discoverable without a monitor.
"""
import json
import os
import socket
import subprocess
import time
import urllib.request

MEOWFARM_PORT = 8888
BROADCAST_INTERVAL = 60
CONSOLE_URL_FILE = "/var/run/mfarm/console_url"


def _save_console_url(url: str) -> None:
    """Record the working console URL so meowos-updater can find it.

    Written to tmpfs (/var/run/mfarm/), so it's regenerated each boot once
    phonehome locates the server again. Tolerant of dir/permission errors —
    the updater treats a missing file as 'not yet known'.
    """
    try:
        os.makedirs(os.path.dirname(CONSOLE_URL_FILE), exist_ok=True)
        with open(CONSOLE_URL_FILE, "w") as f:
            f.write(url)
    except OSError:
        pass


def get_interfaces():
    """Get all network interfaces with IPs and MACs."""
    interfaces = []
    try:
        result = subprocess.run(
            ["ip", "-j", "addr", "show"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for iface in json.loads(result.stdout):
                name = iface.get("ifname", "")
                if name == "lo":
                    continue
                mac = iface.get("address", "")
                for addr_info in iface.get("addr_info", []):
                    if addr_info.get("family") == "inet":
                        interfaces.append({
                            "name": name,
                            "ip": addr_info.get("local", ""),
                            "mac": mac,
                        })
    except Exception:
        pass
    return interfaces


def get_gateway():
    """Get the default gateway IP (likely the router)."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if "default via" in line:
                return line.split("via")[1].strip().split()[0]
    except Exception:
        pass
    return None


def phone_home():
    """Send rig info to MeowFarm server via UDP broadcast and HTTP."""
    hostname = socket.gethostname()
    interfaces = get_interfaces()
    gateway = get_gateway()

    if not interfaces:
        return

    payload = json.dumps({
        "type": "phonehome",
        "hostname": hostname,
        "interfaces": interfaces,
        "gateway": gateway,
    }).encode()

    # Method 1: UDP broadcast on port 8889 (MeowFarm listens). The console
    # replies with its HTTP port; we use the reply's source IP as the console
    # URL. This is the only way to discover a cross-subnet console — HTTP
    # fallback below only probes our own /24.
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))  # ephemeral source port for recvfrom
        sock.settimeout(2.0)
        sock.sendto(payload, ("255.255.255.255", 8889))
        try:
            data, addr = sock.recvfrom(4096)
            reply = json.loads(data.decode())
            if reply.get("type") == "phonehome-reply":
                port = reply.get("port", MEOWFARM_PORT)
                _save_console_url(f"http://{addr[0]}:{port}")
        except (socket.timeout, OSError, ValueError):
            pass
        sock.close()
    except Exception:
        pass

    # Method 2: Try to reach MeowFarm server via gateway subnet scan
    # The server is likely on the same subnet
    if gateway:
        subnet = ".".join(gateway.split(".")[:3])
        # Try common addresses for the MeowFarm server
        for host_part in [gateway.split(".")[-1], "1"]:
            try:
                base = f"http://{subnet}.{host_part}:{MEOWFARM_PORT}"
                req = urllib.request.Request(
                    f"{base}/api/phonehome", data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=3)
                _save_console_url(base)
                break
            except Exception:
                continue


def main():
    while True:
        try:
            phone_home()
        except Exception:
            pass
        time.sleep(BROADCAST_INTERVAL)


if __name__ == "__main__":
    main()
