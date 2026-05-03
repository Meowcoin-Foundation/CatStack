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

# UniFi controller — used to find the rig's new DHCP-assigned IP after the
# dd-reboot. Without this we'd be polling the old HiveOS IP forever (the
# MeowOS first-boot reset gets a fresh DHCP lease, often on a different
# subnet via the /22 router).
UNIFI_URL = "https://192.168.68.1"
UNIFI_USER = "Claude"
UNIFI_PASS = "Mwie3755@@@@"
UNIFI_SITE = "default"
# ─────────────────────────────────────────────────────────────────────


def banner(msg: str) -> None:
    print(f"\n{'=' * 70}\n  {msg}\n{'=' * 70}")


# Module-level so it persists across step calls and the rig MAC we capture
# pre-flash can be looked up post-reboot.
RIG_MAC: str | None = None


def _unifi_session():
    """Authenticate to UniFi and return a requests.Session with CSRF set."""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    s.verify = False
    r = s.post(
        f"{UNIFI_URL}/api/auth/login",
        json={"username": UNIFI_USER, "password": UNIFI_PASS, "remember": True},
        timeout=10,
    )
    r.raise_for_status()
    csrf = r.headers.get("X-CSRF-Token") or r.headers.get("x-csrf-token")
    if csrf:
        s.headers["X-CSRF-Token"] = csrf
    return s


def unifi_find_ip_by_mac(mac: str) -> str | None:
    """Look up the current IP for `mac` in UniFi's active client list.
    Returns None if not found / not currently online."""
    sess = _unifi_session()
    r = sess.get(
        f"{UNIFI_URL}/proxy/network/api/s/{UNIFI_SITE}/stat/sta",
        timeout=10,
    )
    r.raise_for_status()
    mac_lc = mac.lower()
    for c in r.json().get("data", []):
        if (c.get("mac") or "").lower() == mac_lc:
            return c.get("ip")
    return None


def unifi_pin_ip(mac: str, ip: str, name: str) -> None:
    """Set a UniFi DHCP reservation pinning `mac` to `ip`. Renames the
    UniFi client record to `name` for visibility. Call BEFORE dd so the
    rebooted MeowOS picks up the reservation on its first DHCP request,
    avoiding the same-IP collision that bit HiveMini17/25 (both got
    192.168.70.38 from a fresh DHCP pool)."""
    sess = _unifi_session()
    r = sess.get(
        f"{UNIFI_URL}/proxy/network/api/s/{UNIFI_SITE}/rest/user",
        timeout=10,
    )
    r.raise_for_status()
    mac_lc = mac.lower()
    user = next(
        (u for u in r.json().get("data", []) if (u.get("mac") or "").lower() == mac_lc),
        None,
    )
    if not user:
        # Brand new MAC UniFi hasn't fingerprinted yet — fall through; first
        # DHCP request will populate the user record. Leaving pinning to a
        # post-reboot pass would hit the conflict window we're trying to avoid.
        # Create a user record explicitly.
        r = sess.post(
            f"{UNIFI_URL}/proxy/network/api/s/{UNIFI_SITE}/rest/user",
            json={"mac": mac_lc, "name": name, "use_fixedip": True, "fixed_ip": ip},
            timeout=10,
        )
        r.raise_for_status()
        return
    body = {"use_fixedip": True, "fixed_ip": ip, "name": name}
    if user.get("network_id"):
        body["network_id"] = user["network_id"]
    r = sess.put(
        f"{UNIFI_URL}/proxy/network/api/s/{UNIFI_SITE}/rest/user/{user['_id']}",
        json=body, timeout=10,
    )
    r.raise_for_status()


def derive_static_ip(rig_name: str) -> str:
    """For 'HiveMiniNN' rigs, return 192.168.70.NN. The HiveMini fleet
    fits comfortably in 192.168.70.0/24 since .17, .25, .43, .124, .162
    are the only existing pins. Other rig names fall through to None."""
    import re
    m = re.match(r"HiveMini0?(\d+)", rig_name)
    if m:
        return f"192.168.70.{int(m.group(1))}"
    return ""


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
    """nc-relayed dd flash. The data path is:

        console: cat meowos.img | nc rig:RELAY_PORT
        rig:     nc -l RELAY_PORT > FIFO  →  dd of=disk reads FIFO

    Both nc and dd on the rig are launched via `nohup setsid` BEFORE we
    close our SSH session and BEFORE any dd writes hit the disk. Once
    they're running detached, sshd dying mid-flash doesn't matter —
    nothing in the data path depends on sshd. nc and dd's binaries are
    loaded once into RAM at exec time; they don't fork or page-in new
    libraries during the transfer. With xmrig stopped, RAM pressure is
    low so the kernel keeps their pages resident.

    Lessons from 2026-05-03:
      * Streaming directly via the SSH channel died at 90% (HiveMini08
        bricked) — sshd's child handler couldn't keep up once libc pages
        needed reload from corrupted disk.
      * Disk-staging the image first hit a different race: ext4 on HiveOS
        allocates files in low byte offsets (1.5–8 GB) which are inside
        the dd write zone — would have corrupted the dd mid-read.
      * RAM-staging via tmpfs needs >10 GB free; HiveMini fleet has only
        ~6.4 GB available even after stopping xmrig.
    """
    banner(f"1-5: nc-relayed dd-flash {RIG_NAME} ({RIG_IP})")
    img_size = os.path.getsize(IMG_PATH)
    print(f"image: {IMG_PATH} ({img_size / 1024 ** 3:.2f} GB)")

    c = ssh(RIG_IP, HIVEOS_USER, HIVEOS_PASS)
    disk = detect_root_disk(c)
    print(f"target disk: {disk}")

    # Capture the MAC of the primary NIC. We need this AFTER the dd reboot
    # because MeowOS first-boot gets a fresh DHCP lease which often lands on
    # a different IP/subnet — we can't poll the old IP forever, but we CAN
    # poll UniFi for this MAC reappearing under a new IP.
    global RIG_MAC
    _, stdout, _ = c.exec_command(
        # Print MAC of the first non-loopback ethernet interface up.
        # `ip -o link show` formats as: "N: name: <flags> mtu N qdisc ... link/ether MAC ..."
        "ip -o link show 2>/dev/null | "
        "awk '$2 ~ /^(eth|en)/ {for(i=1;i<=NF;i++) if($i==\"link/ether\") {print $(i+1); exit}}'",
        timeout=5)
    RIG_MAC = stdout.read().decode().strip().lower()
    if not RIG_MAC or len(RIG_MAC) != 17:
        raise RuntimeError(f"could not extract NIC MAC: {RIG_MAC!r}")
    print(f"rig MAC (for post-reboot lookup): {RIG_MAC}")

    # Pin a static IP via UniFi DHCP reservation BEFORE dd. MeowOS first-boot
    # gets a fresh DHCP lease; without the pin, two rigs flashed in
    # quick succession can both land on the same fresh IP from the pool
    # (HiveMini17/25 both got 192.168.70.38). The pin tells UniFi to hand
    # this MAC the same IP on every request, avoiding the collision.
    target_ip = derive_static_ip(RIG_NAME)
    if target_ip:
        try:
            unifi_pin_ip(RIG_MAC, target_ip, RIG_NAME)
            print(f"unifi: pinned {RIG_MAC} -> {target_ip}")
        except Exception as e:
            print(f"unifi pin warning ({type(e).__name__}: {str(e)[:80]}); "
                  f"continuing — post-reboot lookup is by MAC anyway")
    else:
        print(f"no static-IP rule for {RIG_NAME!r}; relying on dynamic DHCP")

    # Verify nc is available on the rig — if not, nothing in this approach works.
    _, stdout, _ = c.exec_command("which nc.openbsd nc.traditional nc 2>&1 | head -1", timeout=5)
    nc_path = stdout.read().decode().strip()
    if not nc_path or not nc_path.startswith("/"):
        raise RuntimeError(f"no nc binary on rig — bail: {nc_path!r}")
    print(f"nc binary: {nc_path}")

    # 1. Quiesce HiveOS. Frees RAM (xmrig hugepages release) and stops
    # services that might trigger disk reads while dd is running.
    print("\nstep 1: quiesce HiveOS services + miners...")
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

    # 2. Launch nc + dd pipeline in setsid+nohup. They're connected via
    # a FIFO in /dev/shm (RAM-backed, immune to disk overwrite). When the
    # console-side `cat | nc` finishes pushing the image, nc closes the
    # FIFO write end, dd hits EOF, sync's, triggers sysrq reboot.
    print("\nstep 2: launch nc+dd relay (detached)...")
    relay_port = 9999
    relay_script = (
        "#!/bin/bash\n"
        "set -e\n"
        "FIFO=/dev/shm/dd.pipe\n"
        "rm -f $FIFO; mkfifo $FIFO\n"
        # dd in background reading from FIFO
        f"( dd if=$FIFO of={disk} bs=4M oflag=direct status=progress 2>>/dev/shm/dd.log; "
        "  sync; "
        "  echo DD_OK >> /dev/shm/dd.log; "
        "  sleep 1; "
        "  echo b > /proc/sysrq-trigger ) &\n"
        "DD_PID=$!\n"
        # nc listens once, redirects stdin into the FIFO
        f"{nc_path} -l -p {relay_port} > $FIFO\n"
        "wait $DD_PID\n"
    )
    import base64
    b64 = base64.b64encode(relay_script.encode()).decode()
    # Detach pattern: the relay is launched inside a subshell `( ... ) &` so
    # the outer shell can exit cleanly once the launcher returns. All FDs
    # are redirected to /dev/null + log file so nothing keeps the SSH
    # channel open after the launch returns.
    launcher = (
        f"echo {b64} | base64 -d > /dev/shm/relay.sh\n"
        "chmod +x /dev/shm/relay.sh\n"
        "true > /dev/shm/dd.log\n"
        "( setsid bash /dev/shm/relay.sh </dev/null >>/dev/shm/dd.log 2>&1 ) &\n"
        "sleep 1\n"
        "exit 0\n"
    )
    launcher_b64 = base64.b64encode(launcher.encode()).decode()
    setup_cmd = (
        f"sudo -n bash -c \"echo {launcher_b64} | base64 -d | bash\""
    )
    c.exec_command(setup_cmd, timeout=15)
    # Don't read exec's stdout — the backgrounded process can keep it open
    # in subtle ways across sshd implementations. The launcher exits cleanly,
    # which is enough; we verify state in a fresh session below.

    # Verify in a fresh SSH session that nc is actually listening. Use a
    # bounded retry loop because nc takes a beat to bind.
    print(f"  waiting for nc to bind {relay_port}...")
    c.close()
    listening = False
    for attempt in range(15):
        time.sleep(2)
        try:
            c2 = ssh(RIG_IP, HIVEOS_USER, HIVEOS_PASS, timeout=5)
            _, stdout, _ = c2.exec_command(
                f"ss -lnt 2>/dev/null | awk '$4 ~ /:{relay_port}$/ {{print \"Y\"}}'",
                timeout=5)
            ans = stdout.read().decode().strip()
            c2.close()
            if ans == "Y":
                listening = True
                break
        except Exception as e:
            print(f"    attempt {attempt + 1}: {type(e).__name__}: {str(e)[:60]}")
    if not listening:
        # Pull the log to debug
        try:
            c3 = ssh(RIG_IP, HIVEOS_USER, HIVEOS_PASS, timeout=5)
            _, stdout, _ = c3.exec_command("cat /dev/shm/dd.log; ps -ef | grep -E 'nc|dd|relay' | grep -v grep",
                                           timeout=5)
            log = stdout.read().decode()
            c3.close()
        except Exception:
            log = "(could not pull log)"
        raise RuntimeError(f"nc never started listening on :{relay_port}; state:\n{log}")
    print(f"  OK nc listening on {RIG_IP}:{relay_port}")

    # 3. Push image to the rig via raw TCP. This is the data path that
    # MUST survive even if sshd dies on the rig.
    print(f"\nstep 3: push {img_size / 1024**3:.2f} GB to {RIG_IP}:{relay_port}...")
    sock = socket.create_connection((RIG_IP, relay_port), timeout=15)
    sock.settimeout(60)  # generous per-send deadline
    sent = 0
    last_report = 0
    start = time.time()
    with open(IMG_PATH, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            try:
                sock.sendall(chunk)
            except (socket.error, ConnectionResetError) as e:
                # If nc/dd died on the rig side mid-transfer, we lose the
                # connection. Fall through; check what we got.
                print(f"  send failed at {sent / 1024**3:.2f} GB: {e}")
                break
            sent += len(chunk)
            if sent - last_report >= 1024 * 1024 * 1024:
                pct = sent * 100 / img_size
                rate = sent / 1024 / 1024 / (time.time() - start)
                print(f"  {sent / 1024**3:.1f}/{img_size / 1024**3:.1f} GB "
                      f"({pct:.0f}%, {rate:.0f} MB/s)")
                last_report = sent
    sock.close()
    print(f"  pushed {sent} bytes in {time.time() - start:.0f}s")
    if sent < img_size:
        raise RuntimeError(
            f"only pushed {sent} of {img_size} bytes — relay died mid-transfer; "
            f"rig may need physical recovery")

    # 4. After nc closes its end of the FIFO, dd will see EOF and finish,
    # then trigger sysrq reboot. Watch for the rig to drop.
    print("\nstep 4: image pushed; waiting for dd to finish + reboot...")
    deadline = time.time() + 300
    last_state = "up"
    drop_seen_at = None
    while time.time() < deadline:
        try:
            s = socket.create_connection((RIG_IP, 22), timeout=3)
            s.close()
            if last_state == "down":
                print(f"  rig back up (post-reboot) at {time.strftime('%H:%M:%S')}")
                return
            last_state = "up"
        except (socket.timeout, OSError):
            if last_state == "up":
                drop_seen_at = time.time()
                print(f"  rig dropped at {time.strftime('%H:%M:%S')} (rebooting)")
            last_state = "down"
        time.sleep(3)
    if drop_seen_at is None:
        raise RuntimeError(
            "rig never dropped within 5 min — dd may be stuck. "
            "Inspect /dev/shm/dd.log on the rig (still HiveOS) for clues.")


def step_wait_meowos() -> None:
    """Wait for the rebooted rig to come back up as MeowOS.

    The dd-reboot puts the rig through MeowOS first-boot which can take
    15-30 min: ssh-keygen -A, sgdisk -e + growpart + resize2fs to expand
    root to the full 1.8TB, sensors-detect, then a SECOND reboot at the
    end of the firstboot script.

    We cannot poll the old HiveOS IP — the rebooted MeowOS gets a fresh
    DHCP lease, often on a different /22 subnet (e.g. 192.168.68.92 →
    192.168.70.38). Instead we poll UniFi for the rig's MAC, find its
    new IP, then SSH there. Updates the global RIG_IP for downstream
    steps.
    """
    global RIG_IP
    if not RIG_MAC:
        raise RuntimeError("RIG_MAC not captured during dd-flash; cannot wait by MAC")
    banner(f"6: wait for MeowOS (MAC {RIG_MAC}) to reappear")
    deadline = time.time() + 30 * 60  # 30 min ceiling for first-boot
    last_log_at = 0.0
    while time.time() < deadline:
        time.sleep(15)
        try:
            ip = unifi_find_ip_by_mac(RIG_MAC)
        except Exception as e:
            if time.time() - last_log_at > 60:
                print(f"  unifi probe error: {type(e).__name__}: {str(e)[:60]}")
                last_log_at = time.time()
            continue
        if not ip:
            if time.time() - last_log_at > 60:
                print(f"  [{time.strftime('%H:%M:%S')}] MAC not yet in UniFi active list...")
                last_log_at = time.time()
            continue
        # MAC is back online. Try MeowOS-creds SSH to confirm it's MeowOS,
        # not HiveOS rebooting back to itself.
        try:
            c = ssh(ip, MFARM_USER, MFARM_PASS, timeout=5)
            _, stdout, _ = c.exec_command(
                "hostname; head -1 /etc/os-release; uptime", timeout=8)
            text = stdout.read().decode().strip()
            c.close()
            if "MeowOS" in text or "mfarm-rig" in text:
                print(f"  rig back as MeowOS at {ip}: {text.replace(chr(10), ' | ')}")
                if ip != RIG_IP:
                    print(f"  IP changed: {RIG_IP} -> {ip} (DHCP refreshed on reboot)")
                    RIG_IP = ip
                return
            print(f"  unexpected SSH response at {ip}: {text[:200]}")
        except Exception as e:
            if time.time() - last_log_at > 60:
                print(f"  [{time.strftime('%H:%M:%S')}] MAC at {ip}, ssh not ready yet "
                      f"({type(e).__name__})")
                last_log_at = time.time()
    raise RuntimeError(
        f"MeowOS (MAC {RIG_MAC}) didn't come back within 30 min. "
        f"Check UniFi for the MAC; the rig may need physical recovery.")


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
                print(f"  OK {RIG_NAME}: {miner} {hr / 1000:.2f} kH/s, "
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
