"""Migrate a HiveOS rig to CatStack/MeowOS, applying a chosen flight sheet.

Usage:
    python _migrate-hive-to-catstack.py <rig_name> <rig_ip> [<flight_sheet>]

Defaults: flight sheet = "XTM_Working" (RandomX → Kryptex via xmrig).

Pipeline (each step verified before proceeding):
  1. SSH to HiveOS rig as user/1 (HiveOS default).
  2. Detect root disk (handles nvme0n1 + sda).
  3. Quiesce hive-* services + kill any miner.
  4. Stream meowos.img into `dd` over SSH (~5-10 min on GbE depending on disk).
  5. sysrq force-reboot.
  6. Wait for MeowOS to boot back up at the same IP (UniFi DHCP is sticky
     by MAC, so the IP holds across the OS swap).
  7. POST /api/rigs to register the rig.
  8. POST /api/rigs/<name>/push-key (auto-bootstrap CatStack's SSH key).
  9. POST /api/flightsheets/<fs>/apply/<name>.
 10. Verify miner is hashing within 90s.

This script targets HiveOS specifically. The `_flash-and-onboard.py` next to
it targets the same flow but assumes /dev/sda + hardcodes LPEPE — kept around
for the legacy ccminer-first rigs. For HiveMini fleet migration use this one.
"""
import io
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

import paramiko

# ── CONFIG ────────────────────────────────────────────────────────────
RIG_NAME = sys.argv[1] if len(sys.argv) > 1 else "HiveMini08"
RIG_IP = sys.argv[2] if len(sys.argv) > 2 else "192.168.68.21"
FLIGHT_SHEET = sys.argv[3] if len(sys.argv) > 3 else "XTM_Working"

CATSTACK_API = "http://192.168.68.78:8888"
KEY_PATH = os.path.expanduser("~/.ssh/id_ed25519")
IMG_PATH = r"C:\Source\meowos.img"
HIVEOS_USER = "user"
HIVEOS_PASS = "1"
MFARM_USER = "miner"
MFARM_PASS = "mfarm"
# ─────────────────────────────────────────────────────────────────────


def banner(msg: str) -> None:
    print(f"\n{'=' * 70}\n  {msg}\n{'=' * 70}")


def ssh(host: str, user: str, password: str | None = None, timeout: int = 10) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if password is not None:
        c.connect(host, username=user, password=password, timeout=timeout,
                  allow_agent=False, look_for_keys=False,
                  banner_timeout=timeout, auth_timeout=timeout)
    else:
        c.connect(host, username=user, key_filename=KEY_PATH, timeout=timeout,
                  banner_timeout=timeout, auth_timeout=timeout)
    return c


def detect_root_disk(c: paramiko.SSHClient) -> str:
    """Return the parent block device of /, e.g. /dev/nvme0n1 or /dev/sda."""
    _, stdout, _ = c.exec_command(
        "sudo -n bash -c 'pk=$(lsblk -no PKNAME $(findmnt -no SOURCE /) | head -1); echo /dev/$pk'",
        timeout=10,
    )
    disk = stdout.read().decode().strip()
    if not disk.startswith("/dev/") or len(disk) < 7:
        raise RuntimeError(f"could not detect root disk (got {disk!r})")
    return disk


def step_dd_flash() -> None:
    banner(f"1-5: dd-flash {RIG_NAME} ({RIG_IP})")
    img_size = os.path.getsize(IMG_PATH)
    print(f"image: {IMG_PATH} ({img_size / 1024 / 1024 / 1024:.2f} GB)")

    c = ssh(RIG_IP, HIVEOS_USER, HIVEOS_PASS)
    disk = detect_root_disk(c)
    print(f"target disk: {disk}")

    # Quiesce HiveOS so it isn't fighting writes / scribbling stats during dd.
    print("quiescing HiveOS services and miners...")
    _, stdout, _ = c.exec_command(
        "sudo -n bash -c '"
        "systemctl stop hive hive-watchdog hive-console hive-ttyd hive-flash 2>/dev/null; "
        "pkill -KILL -f xmrig 2>/dev/null; "
        "pkill -KILL -f miner 2>/dev/null; "
        "pkill -KILL SCREEN 2>/dev/null; "
        "sleep 2; sync; "
        "echo 3 > /proc/sys/vm/drop_caches; "
        "echo quiesced'", timeout=30)
    print(f"  {stdout.read().decode().strip()}")

    # Stream the image into dd. We open a fresh exec channel and shovel bytes
    # into stdin; sudo /bin/sh -c gives us a shell with the right perms to
    # write to a raw block device.
    print(f"streaming {img_size / 1024 / 1024 / 1024:.1f} GB to {disk} (~5-10 min)...")
    chan = c.get_transport().open_session()
    chan.exec_command(f"sudo -n /bin/sh -c 'dd of={disk} bs=4M oflag=direct status=progress'")
    sent = 0
    last_report = 0
    start = time.time()
    with open(IMG_PATH, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            chan.sendall(chunk)
            sent += len(chunk)
            if sent - last_report >= 1024 * 1024 * 1024:
                pct = sent * 100 / img_size
                rate = sent / 1024 / 1024 / (time.time() - start)
                print(f"  {sent / 1024 ** 3:.1f}/{img_size / 1024 ** 3:.1f} GB "
                      f"({pct:.0f}%, {rate:.0f} MB/s)")
                last_report = sent
    chan.shutdown_write()

    print("flushing...")
    err = b""
    while True:
        if chan.recv_stderr_ready():
            err += chan.recv_stderr(4096)
        if chan.exit_status_ready():
            break
        time.sleep(0.5)
    rc = chan.recv_exit_status()
    last_line = err.decode(errors="replace").strip().splitlines()[-1] if err else ""
    print(f"  dd exit={rc} :: {last_line[:120]}")
    if rc != 0:
        raise RuntimeError(f"dd to {disk} failed — investigate before proceeding")

    print("force-rebooting via sysrq...")
    # Don't wait for response — kernel is going down.
    try:
        c.exec_command(
            "sudo -n /bin/sh -c 'sync; sleep 1; echo b > /proc/sysrq-trigger'",
            timeout=5)
    except Exception:
        pass
    c.close()


def step_wait_meowos() -> None:
    banner(f"6: wait for MeowOS to come up at {RIG_IP}")
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(8)
        try:
            c = ssh(RIG_IP, MFARM_USER, MFARM_PASS, timeout=4)
            _, stdout, _ = c.exec_command(
                "hostname; head -1 /etc/os-release", timeout=5)
            text = stdout.read().decode().strip()
            c.close()
            # MeowOS images: hostname starts mfarm-rig-<mac>; or hostname has been
            # synced to the rig name; either way the OS line will show Ubuntu (the
            # base) — the discriminator is that this user (miner@mfarm) only
            # exists on MeowOS, not HiveOS.
            print(f"  UP: {text.replace(chr(10), ' | ')}")
            return
        except Exception:
            pass
    raise RuntimeError("MeowOS didn't come back within 5 min — manual recovery needed")


def step_register() -> None:
    banner(f"7-8: register {RIG_NAME} in CatStack + push-key")
    body = json.dumps({"name": RIG_NAME, "host": RIG_IP}).encode()
    req = urllib.request.Request(
        f"{CATSTACK_API}/api/rigs", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"  registered {RIG_NAME}")
    except urllib.error.HTTPError as e:
        msg = e.read()
        if e.code == 400 and b"already exists" in msg:
            print(f"  {RIG_NAME} already in CatStack — continuing")
        else:
            raise RuntimeError(f"register failed: HTTP {e.code} {msg!r}")

    # push-key. SSH banner errors are common immediately after first boot
    # because sshd is rate-limiting; retry up to 4 times.
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                f"{CATSTACK_API}/api/rigs/{RIG_NAME}/push-key", method="POST")
            r = urllib.request.urlopen(req, timeout=30).read()
            data = json.loads(r)
            if "pushed" in data.get("status", ""):
                print(f"  push-key (attempt {attempt + 1}): {data.get('message')}")
                return
            last_err = data
        except urllib.error.HTTPError as e:
            try:
                last_err = json.loads(e.read())
            except Exception:
                last_err = str(e)
        print(f"  push-key attempt {attempt + 1}: {last_err}")
        time.sleep(15)
    raise RuntimeError(f"push-key failed after 4 attempts: {last_err}")


def step_apply_flightsheet() -> None:
    banner(f"9: apply flight sheet '{FLIGHT_SHEET}' to {RIG_NAME}")
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                f"{CATSTACK_API}/api/flightsheets/{FLIGHT_SHEET}/apply/{RIG_NAME}",
                method="POST")
            r = urllib.request.urlopen(req, timeout=60).read()
            data = json.loads(r)
            res = data.get("results", {}).get(RIG_NAME, "")
            if res == "applied":
                print(f"  applied (attempt {attempt + 1})")
                return
            last = res
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        print(f"  attempt {attempt + 1}: {last}")
        time.sleep(15)
    raise RuntimeError(f"apply {FLIGHT_SHEET} failed after 4 attempts: {last}")


def step_verify_mining() -> None:
    banner("10: verify miner is hashing")
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(
                f"{CATSTACK_API}/api/rigs/{RIG_NAME}/stats", timeout=8)
            d = json.loads(r.read())
            m = d.get("miner") or {}
            cm = d.get("cpu_miner") or {}
            hr = (m.get("hashrate") or 0) + (cm.get("hashrate") or 0)
            if hr > 100 or (m.get("accepted") or 0) > 0 or (cm.get("accepted") or 0) > 0:
                miner = m.get("name") or cm.get("name") or "?"
                print(f"  ✓ {RIG_NAME}: {miner} {hr / 1000:.2f} kH/s, "
                      f"running={m.get('running') or cm.get('running')}")
                return
        except Exception as e:
            print(f"  poll error: {e}")
        time.sleep(10)
    print("  WARN: hashrate not seen within 120s — agent may still be ramping; "
          "check `miner` output on the rig manually")


def main() -> None:
    try:
        step_dd_flash()
        step_wait_meowos()
        step_register()
        step_apply_flightsheet()
        step_verify_mining()
        banner(f"DONE — {RIG_NAME} is on MeowOS / CatStack running {FLIGHT_SHEET}")
    except Exception as e:
        banner(f"FAILED at: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
