#!/usr/bin/env python3
"""
MeowOS Auto-Updater
Pulls the latest agent bundle from the CatStack console and installs it.
Runs on a 5-minute systemd timer (and 60s after boot).

Trust model: plain HTTP on a trusted LAN, matching the phonehome posture.
The updater refuses any tar entry outside opt/mfarm/, etc/systemd/system/,
or etc/profile.d/, and refuses paths containing '..'.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

CONSOLE_URL_FILE = "/var/run/mfarm/console_url"
LOCAL_VERSION_FILE = "/opt/mfarm/VERSION"
LOG_FILE = "/var/log/mfarm/updater.log"
STAGING = "/tmp/mfarm-update-staging"
ALLOWED_PREFIXES = ("opt/mfarm/", "etc/systemd/system/", "etc/profile.d/")

# Services restarted after the file swap. The updater itself isn't restarted —
# Linux keeps the old inode for the running process and the next timer fire
# picks up the new file.
SERVICES_TO_RESTART = (
    "mfarm-agent.service",
    "meowos-phonehome.service",
    "meowos-webui.service",
)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    sys.stderr.write(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except OSError:
        pass


def parse_version(s: str) -> tuple:
    out = []
    for p in s.strip().split("."):
        try:
            out.append(int(p))
        except ValueError:
            return ()
    return tuple(out)


def read_local_version() -> str:
    try:
        with open(LOCAL_VERSION_FILE) as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def read_console_url() -> str | None:
    try:
        with open(CONSOLE_URL_FILE) as f:
            url = f.read().strip().rstrip("/")
        return url or None
    except OSError:
        return None


def http_get(url: str, timeout: int):
    req = urllib.request.Request(url, headers={"User-Agent": "meowos-updater/1"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_remote_version(console: str) -> str:
    with http_get(f"{console}/api/agent/version", timeout=10) as r:
        return json.loads(r.read().decode())["version"]


def fetch_bundle(console: str) -> bytes:
    with http_get(f"{console}/api/agent/bundle", timeout=180) as r:
        return r.read()


def safe_extract(tar_bytes: bytes, dest: str) -> list[str]:
    """Extract tarball to dest. Returns list of relative paths extracted.
    Raises ValueError on any path outside ALLOWED_PREFIXES or containing '..'.
    Modes from the tarball are ignored — install_files sets them by suffix
    (the server may pack on Windows, where unix mode bits are unreliable)."""
    extracted: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            n = m.name
            if n.startswith("./"):
                n = n[2:]
            parts = Path(n).parts
            if not parts or ".." in parts or n.startswith("/"):
                raise ValueError(f"unsafe path in bundle: {m.name}")
            if not any(n.startswith(p) for p in ALLOWED_PREFIXES):
                raise ValueError(f"disallowed path in bundle: {m.name}")
            target = Path(dest) / n
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(n)
    return extracted


def install_files(staging_dir: str, files: list[str]) -> None:
    """Move staged files into '/'. Per-file atomic via os.replace; falls back
    to shutil.move across filesystems. Modes set by suffix: 0o755 for scripts
    and ELF binaries, 0o644 for everything else (binary detection is by ELF
    magic so files like fan_controller_cli that have no extension still get
    +x, while data blobs alongside them stay 0o644)."""
    for rel in files:
        src = Path(staging_dir) / rel
        dst = Path("/") / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dst)
        except OSError:
            shutil.move(str(src), str(dst))
        mode = 0o644
        if rel.endswith((".py", ".sh")):
            mode = 0o755
        else:
            try:
                with open(dst, "rb") as f:
                    if f.read(4) == b"\x7fELF":
                        mode = 0o755
            except OSError:
                pass
        try:
            os.chmod(dst, mode)
        except OSError:
            pass


def restart_services() -> None:
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    for svc in SERVICES_TO_RESTART:
        subprocess.run(["systemctl", "restart", svc], check=False)
    # Idempotent — covers a fresh rig that didn't have the timer enabled yet
    # (e.g. updated from a pre-updater MeowOS image).
    subprocess.run(
        ["systemctl", "enable", "--now", "meowos-updater.timer"], check=False
    )
    # New defensive units shipped via the bundle: enable them on first
    # extraction so the rig benefits without needing the operator to
    # manually `systemctl enable`. Idempotent.
    for unit in ("meowos-dpkg-health.service",):
        subprocess.run(["systemctl", "enable", unit], check=False)


def main() -> int:
    console = read_console_url()
    if not console:
        log("no console URL known yet (phonehome hasn't reached server) — skipping")
        return 0

    local = read_local_version()
    try:
        remote = fetch_remote_version(console)
    except Exception as e:
        log(f"version check failed: {e}")
        return 1

    if parse_version(remote) <= parse_version(local):
        log(f"up to date (local={local} remote={remote})")
        return 0

    log(f"updating {local} -> {remote} from {console}")

    try:
        bundle = fetch_bundle(console)
    except Exception as e:
        log(f"bundle fetch failed: {e}")
        return 1

    shutil.rmtree(STAGING, ignore_errors=True)
    os.makedirs(STAGING, exist_ok=True)

    try:
        files = safe_extract(bundle, STAGING)
    except Exception as e:
        log(f"extract failed: {e}")
        return 1

    if "opt/mfarm/VERSION" not in files:
        log("bundle missing VERSION marker — refusing to install")
        return 1

    try:
        install_files(STAGING, files)
    except Exception as e:
        # Mid-flight failure: some files swapped, others not. Services restart
        # below will pick up whatever's on disk; next timer retries.
        log(f"install failed mid-flight: {e}")
        return 1

    shutil.rmtree(STAGING, ignore_errors=True)
    restart_services()
    log(f"updated to {remote}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
