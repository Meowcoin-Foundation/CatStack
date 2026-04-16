#!/usr/bin/env python3
"""
MeowOS Phone-Home Service
Broadcasts this rig's IP, MAC, and hostname to the MeowFarm server every 60 seconds.
Runs as a systemd service so the rig is discoverable without a monitor.
"""
import json
import socket
import subprocess
import time
import urllib.request

MEOWFARM_PORT = 8888
BROADCAST_INTERVAL = 60


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

    # Method 1: UDP broadcast on port 8889 (MeowFarm listens)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(payload, ("255.255.255.255", 8889))
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
                url = f"http://{subnet}.{host_part}:{MEOWFARM_PORT}/api/phonehome"
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=3)
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
