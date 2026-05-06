"""End-to-end: dd-flash a HiveOS rig + onboard to CatStack + apply LPEPE.

Usage: edit RIG_NAME / RIG_IP below and run.

Steps (each step verified before the next):
  1. SSH to HiveOS rig (user/1)
  2. Quiesce services + drop caches
  3. Stream meowos.img into dd of=/dev/sda (~2 min over GbE)
  4. Sync + sysrq force-reboot
  5. Poll for MeowOS to boot back up at the same IP (UniFi-pinned)
  6. POST /api/rigs to register
  7. push-key (uses miner+mfarm)
  8. SCP ccminer binary from rig02 (image bundle missing it)
  9. Apply LPEPE flight sheet
 10. Verify ccminer running + hashrate
"""
import paramiko, time, os, sys, socket, urllib.request, json, io

# ── CONFIG ────────────────────────────────────────────────────────────
# Override via CLI args: python _flash-and-onboard.py <name> <ip>
import sys as _sys
RIG_NAME = _sys.argv[1] if len(_sys.argv) > 1 else "HIVE03"
RIG_IP   = _sys.argv[2] if len(_sys.argv) > 2 else "192.168.68.171"
# ─────────────────────────────────────────────────────────────────────

KEY  = r"C:\Users\benef\.ssh\id_ed25519"
IMG  = r"C:\Source\meowos.img"
CCMINER_SOURCE = "192.168.68.33"  # rig02 — has working ccminer
CONSOLE = "http://192.168.68.78:8888"


def banner(msg):
    print(f"\n{'=' * 60}\n  {msg}\n{'=' * 60}")


def ssh(host, user="root", pw=None, timeout=10):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if pw:
        c.connect(host, username=user, password=pw, timeout=timeout,
                  allow_agent=False, look_for_keys=False)
    else:
        c.connect(host, username=user, key_filename=KEY, timeout=timeout)
    return c


def step_dd_flash():
    banner(f"1-4: dd-flash {RIG_NAME} ({RIG_IP})")
    img_size = os.path.getsize(IMG)
    print(f"image: {IMG} ({img_size/1024/1024/1024:.2f} GB)")

    c = ssh(RIG_IP, "user", "1")

    # Quiesce
    print("quiescing services...")
    _, stdout, _ = c.exec_command(
        "sudo -n /bin/bash -c '"
        "systemctl stop hive-agent hive-watchdog hive-flash 2>/dev/null; "
        "pkill -KILL -f ccminer 2>/dev/null; "
        "pkill -KILL -f miner 2>/dev/null; "
        "sleep 2; sync; "
        "echo 3 > /proc/sys/vm/drop_caches; "
        "echo quiesced'", timeout=30)
    print(f"  {stdout.read().decode().strip()}")

    # Stream dd
    print("starting dd (~2 min)...")
    chan = c.get_transport().open_session()
    chan.exec_command("sudo -n /bin/sh -c 'dd of=/dev/sda bs=4M oflag=direct status=progress'")
    sent = 0
    last_report = 0
    start = time.time()
    with open(IMG, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            chan.sendall(chunk)
            sent += len(chunk)
            if sent - last_report >= 1024 * 1024 * 1024:
                pct = sent * 100 / img_size
                rate = sent / 1024 / 1024 / (time.time() - start)
                print(f"  {sent/1024/1024/1024:.1f}/{img_size/1024/1024/1024:.1f} GB ({pct:.0f}%, {rate:.0f} MB/s)")
                last_report = sent
    chan.shutdown_write()

    # Wait for dd exit
    print("flushing...")
    err = b""
    while True:
        if chan.recv_stderr_ready():
            err += chan.recv_stderr(4096)
        if chan.exit_status_ready():
            break
        time.sleep(0.5)
    rc = chan.recv_exit_status()
    last_line = err.decode(errors='replace').strip().splitlines()[-1] if err else ""
    print(f"  dd exit={rc} :: {last_line[:120]}")
    if rc != 0:
        raise RuntimeError("dd failed — investigate before proceeding")

    # Force reboot
    print("force-rebooting via sysrq...")
    c.exec_command("sudo -n /bin/sh -c 'sync; sleep 1; echo b > /proc/sysrq-trigger'", timeout=5)
    c.close()


def step_wait_meowos():
    banner(f"5: wait for MeowOS to boot back up at {RIG_IP}")
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(8)
        try:
            c = ssh(RIG_IP, "miner", "mfarm", timeout=4)
            _, stdout, _ = c.exec_command("hostname; cat /etc/os-release | grep PRETTY_NAME")
            text = stdout.read().decode().strip()
            c.close()
            if "MeowOS" in text or "mfarm-rig" in text:
                print(f"  UP: {text.replace(chr(10), ' | ')}")
                return
        except Exception:
            pass
    raise RuntimeError("MeowOS didn't come back within 5 min — manual recovery needed")


def step_register():
    banner("6-7: register in CatStack + push-key")
    body = json.dumps({"name": RIG_NAME, "host": RIG_IP}).encode()
    req = urllib.request.Request(f"{CONSOLE}/api/rigs", data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"  registered as {RIG_NAME}")
    except urllib.error.HTTPError as e:
        if e.code == 400 and b"already exists" in e.read():
            print(f"  {RIG_NAME} already in CatStack - continuing")
        else:
            raise

    # push-key can fail with SSH banner errors when sshd is rate-limiting
    # post-firstboot. Retry up to 4 times with 15s gaps.
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(f"{CONSOLE}/api/rigs/{RIG_NAME}/push-key", method="POST")
            r = urllib.request.urlopen(req, timeout=30).read()
            data = json.loads(r)
            if "pushed" in data.get("status", ""):
                print(f"  push-key (attempt {attempt+1}): {data.get('message')}")
                return
            last_err = data
        except urllib.error.HTTPError as e:
            try:
                last_err = json.loads(e.read())
            except Exception:
                last_err = str(e)
            print(f"  push-key attempt {attempt+1}: {last_err}")
        time.sleep(15)
    raise RuntimeError(f"push-key failed after 4 attempts: {last_err}")


def step_install_ccminer():
    banner("8: copy ccminer binary from rig02")
    src = ssh(CCMINER_SOURCE)
    sftp = src.open_sftp()
    buf = io.BytesIO()
    sftp.getfo("/opt/mfarm/miners/ccminer", buf)
    sftp.close()
    src.close()
    size_mb = buf.tell() / 1024 / 1024
    print(f"  fetched {size_mb:.1f} MB from rig02")

    # SCP can hit SSH banner errors after rapid post-firstboot attempts.
    # Retry up to 4 times with backoff.
    for attempt in range(4):
        try:
            dst = ssh(RIG_IP, timeout=15)
            sftp = dst.open_sftp()
            buf.seek(0)
            sftp.putfo(buf, "/opt/mfarm/miners/ccminer")
            sftp.chmod("/opt/mfarm/miners/ccminer", 0o755)
            sftp.close()
            dst.exec_command("systemctl restart mfarm-agent", timeout=10)[1].read()
            dst.close()
            print(f"  installed {size_mb:.1f} MB on attempt {attempt+1}")
            return
        except Exception as e:
            print(f"  attempt {attempt+1}: {type(e).__name__}: {e}; retrying in 15s")
            time.sleep(15)
    raise RuntimeError("SCP ccminer failed after 4 attempts")


def step_apply_lpepe():
    banner("9: apply LPEPE flight sheet")
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(f"{CONSOLE}/api/flightsheets/LPEPE/apply/{RIG_NAME}", method="POST")
            r = urllib.request.urlopen(req, timeout=30).read()
            data = json.loads(r)
            res = data.get("results", {}).get(RIG_NAME, "")
            if res == "applied":
                print(f"  apply (attempt {attempt+1}): {res}")
                return
            last = res
            print(f"  attempt {attempt+1}: {res}; retrying")
        except Exception as e:
            last = str(e)
            print(f"  attempt {attempt+1}: {e}; retrying")
        time.sleep(15)
    raise RuntimeError(f"apply LPEPE failed after 4 attempts: {last}")


def step_verify_mining():
    banner("10: verify ccminer hashing")
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(f"{CONSOLE}/api/rigs/{RIG_NAME}/stats", timeout=5)
            d = json.loads(r.read())
            m = d.get("miner") or {}
            hr = m.get("hashrate") or 0
            if hr > 100 or (m.get("accepted") or 0) > 0:
                print(f"  ✓ {RIG_NAME}: {hr/1000:.2f} kH/s, {len(d.get('gpus',[]))} GPUs, "
                      f"running={m.get('running')}, pid={m.get('pid')}")
                return
        except Exception as e:
            print(f"  poll error: {e}")
        time.sleep(8)
    print("  WARN: hashrate not seen in 90s — agent might still be ramping up; check manually")


def main():
    try:
        step_dd_flash()
        step_wait_meowos()
        step_register()
        step_install_ccminer()
        step_apply_lpepe()
        step_verify_mining()
        banner(f"DONE — {RIG_NAME} is mining LPEPE")
    except Exception as e:
        banner(f"FAILED at: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
