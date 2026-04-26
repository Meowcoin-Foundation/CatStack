"""REST API routes for MFarm web dashboard."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import paramiko
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mfarm.db.connection import get_db
from mfarm.db.models import Rig, FlightSheet, OcProfile, Group
from mfarm.ssh.pool import get_pool
from mfarm.miners.registry import list_miners

router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=5)


# ── Rigs ─────────────────────────────────────────────────────────────

class RigCreate(BaseModel):
    name: str
    host: str
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_key_path: str | None = None
    group: str | None = None
    notes: str | None = None

class RigUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    ssh_port: int | None = None
    ssh_user: str | None = None
    ssh_key_path: str | None = None
    group: str | None = None
    notes: str | None = None


@router.get("/rigs")
def get_rigs():
    db = get_db()
    rigs = Rig.get_all(db)
    return [_rig_to_dict(r) for r in rigs]


@router.post("/rigs")
def create_rig(data: RigCreate):
    db = get_db()
    if Rig.get_by_name(db, data.name):
        raise HTTPException(400, f"Rig '{data.name}' already exists")

    group_id = None
    if data.group:
        grp = Group.get_by_name(db, data.group)
        if not grp:
            raise HTTPException(400, f"Group '{data.group}' not found")
        group_id = grp.id

    rig = Rig(name=data.name, host=data.host, ssh_port=data.ssh_port,
              ssh_user=data.ssh_user, ssh_key_path=data.ssh_key_path,
              group_id=group_id, notes=data.notes)
    rig.save(db)

    # Auto-dismiss any discovered rig whose IP matches what we just added.
    # The dismissed-macs set otherwise gets populated by the next /discovered
    # poll, but that's a 15-second window where the popup keeps re-rendering.
    try:
        from mfarm.web.app import _discovered_rigs, _dismissed_macs, _save_dismissed_macs
        changed = False
        for mac, info in _discovered_rigs.items():
            if info.get("ip") == data.host and mac not in _dismissed_macs:
                _dismissed_macs.add(mac)
                changed = True
        if changed:
            _save_dismissed_macs(_dismissed_macs)
    except Exception:
        pass

    return _rig_to_dict(Rig.get_by_name(db, data.name))


@router.put("/rigs/{name}")
def update_rig(name: str, data: RigUpdate):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")
    if data.name is not None:
        existing = Rig.get_by_name(db, data.name)
        if existing and existing.id != rig.id:
            raise HTTPException(400, f"Rig '{data.name}' already exists")
        rig.name = data.name
    if data.host is not None:
        rig.host = data.host
    if data.ssh_port is not None:
        rig.ssh_port = data.ssh_port
    if data.ssh_user is not None:
        rig.ssh_user = data.ssh_user
    if data.ssh_key_path is not None:
        rig.ssh_key_path = data.ssh_key_path
    if data.group is not None:
        if data.group:
            grp = Group.get_by_name(db, data.group)
            if not grp:
                raise HTTPException(400, f"Group '{data.group}' not found")
            rig.group_id = grp.id
        else:
            rig.group_id = None
    if data.notes is not None:
        rig.notes = data.notes
    rig.save(db)
    return _rig_to_dict(rig)


@router.delete("/rigs/{name}")
def delete_rig(name: str):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")
    rig.delete(db)
    return {"status": "deleted"}


@router.get("/rigs/{name}/stats")
async def get_rig_stats(name: str):
    """Fetch fresh stats from a rig on demand."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    pool = get_pool()
    loop = asyncio.get_event_loop()
    try:
        stdout, _, rc = await loop.run_in_executor(
            _executor, lambda r=rig: pool.exec(r, "cat /var/run/mfarm/stats.json", timeout=5)
        )
        if rc == 0 and stdout.strip():
            return json.loads(stdout)
    except Exception:
        pass
    return {}


@router.post("/rigs/{name}/reboot")
async def reboot_rig(name: str):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    pool = get_pool()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_executor, lambda: pool.exec(rig, "reboot", timeout=5))
    except Exception:
        pass
    return {"status": "rebooting"}


@router.post("/rigs/{name}/exec")
async def exec_on_rig(name: str, body: dict):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    command = body.get("command", "")
    if not command:
        raise HTTPException(400, "No command provided")
    pool = get_pool()
    loop = asyncio.get_event_loop()
    try:
        stdout, stderr, rc = await loop.run_in_executor(
            _executor, lambda: pool.exec(rig, command, timeout=30)
        )
        return {"stdout": stdout, "stderr": stderr, "exit_code": rc}
    except Exception as e:
        return {"error": str(e)}


@router.post("/rigs/{name}/restart-miner")
async def restart_miner(name: str):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    pool = get_pool()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            _executor,
            lambda: pool.upload_string(rig, "restart_miner", "/var/run/mfarm/command")
        )
        return {"status": "restart_sent"}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/rigs/{name}/stop-miner")
async def stop_miner(name: str):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    pool = get_pool()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            _executor,
            lambda: pool.upload_string(rig, "stop_miner", "/var/run/mfarm/command")
        )
        return {"status": "stop_sent"}
    except Exception as e:
        raise HTTPException(500, str(e))


# Default fallback credential for freshly-flashed MeowOS rigs. The miner user
# has NOPASSWD sudo, so this gives root SSH access too. If an operator hardens
# their image they should override this via the MEOWOS_DEFAULT_USER /
# MEOWOS_DEFAULT_PASSWORD env vars.
import os as _os

_DEFAULT_BOOTSTRAP_USER = _os.environ.get("MEOWOS_DEFAULT_USER", "miner")
_DEFAULT_BOOTSTRAP_PASSWORD = _os.environ.get("MEOWOS_DEFAULT_PASSWORD", "mfarm")


def _find_dashboard_pubkey() -> str | None:
    """Locate a public key the dashboard can deploy to new rigs.

    Searches common SSH key paths. Returns the key text (one line, no trailing
    newline) or None if nothing is found. Does NOT generate a new key — that's
    too aggressive a default.
    """
    from pathlib import Path
    candidates = []
    home = Path.home()
    for stem in ("id_ed25519", "id_ecdsa", "id_rsa"):
        candidates.append(home / ".ssh" / f"{stem}.pub")
    # System-wide install fallback
    candidates.append(Path("/etc/mfarm/dashboard_id_ed25519.pub"))
    for path in candidates:
        try:
            if path.is_file():
                key = path.read_text().strip()
                if key.startswith(("ssh-", "ecdsa-")):
                    return key
        except Exception:
            continue
    return None


def _bootstrap_root_ssh(host: str, port: int, pubkey: str) -> tuple[bool, str]:
    """Push our public key to root@host using miner+password+sudo.

    Returns (success, message). Used by /push-key endpoint and by
    apply_flightsheet's AuthenticationException fallback. Synchronous —
    callers should run in an executor.
    """
    import paramiko as _paramiko
    client = _paramiko.SSHClient()
    client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=port,
            username=_DEFAULT_BOOTSTRAP_USER,
            password=_DEFAULT_BOOTSTRAP_PASSWORD,
            timeout=10, allow_agent=False, look_for_keys=False,
        )
    except _paramiko.AuthenticationException:
        return False, (
            f"Could not authenticate as '{_DEFAULT_BOOTSTRAP_USER}' with the default "
            f"MeowOS password. The rig was flashed with a custom image — push the "
            f"key manually with `ssh-copy-id root@{host}`."
        )
    except Exception as e:
        return False, f"SSH connect failed: {e}"

    # Use sudo (NOPASSWD on standard MeowOS) to overwrite root's authorized_keys.
    # Single line in the heredoc — escaping the public key safely.
    safe = pubkey.replace("'", "'\"'\"'")
    cmd = (
        "sudo mkdir -p /root/.ssh && "
        "sudo chmod 700 /root/.ssh && "
        f"echo '{safe}' | sudo tee -a /root/.ssh/authorized_keys >/dev/null && "
        "sudo chmod 600 /root/.ssh/authorized_keys && "
        "sudo chown root:root /root/.ssh /root/.ssh/authorized_keys"
    )
    try:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=15)
        rc = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        client.close()
        if rc != 0:
            return False, f"sudo failed (rc={rc}): {err or 'no stderr'}"
        return True, f"Key deployed to root@{host}"
    except Exception as e:
        try: client.close()
        except: pass
        return False, f"sudo exec failed: {e}"


# ── Miner repair / staleness fixup ──────────────────────────────────

# Embedded shell snippet that walks /opt/mfarm/miners/ and re-extracts any
# binary that's actually a gzipped tarball (magic 1f 8b). Older MeowOS images
# shipped with a downloader that fell through to `mv archive.tar.gz xmrig`
# when /usr/bin/file wasn't installed, leaving the rig with an unrunnable
# blob and a "[Errno 8] Exec format error" loop in agent.log. Idempotent —
# binaries that already start with ELF (7f 45 4c 46) are skipped.
_REPAIR_MINERS_SCRIPT = r"""
set -u
fixed=0
for binname in xmrig t-rex lolMiner miniZ rigel cpuminer ccminer; do
    path=/opt/mfarm/miners/$binname
    [ -f "$path" ] || continue
    magic=$(head -c 4 "$path" 2>/dev/null | od -An -tx1 | tr -d ' \n')
    case "$magic" in
        7f454c46) continue ;;  # ELF — already a real binary
        1f8b*)
            tmp=$(mktemp -d)
            cp "$path" "$tmp/archive.tar.gz"
            (cd "$tmp" && tar xzf archive.tar.gz 2>/dev/null) || { rm -rf "$tmp"; continue; }
            bin=$(find "$tmp" -type f -name "$binname" -executable 2>/dev/null | head -1)
            [ -z "$bin" ] && bin=$(find "$tmp" -type f -executable -size +500k 2>/dev/null | head -1)
            if [ -n "$bin" ] && [ "$(head -c 4 "$bin" | od -An -tx1 | tr -d ' \n')" = "7f454c46" ]; then
                sudo cp "$bin" "$path"
                sudo chmod +x "$path"
                echo "REPAIRED: $path"
                fixed=$((fixed + 1))
            fi
            rm -rf "$tmp"
            ;;
    esac
done
echo "repair-summary: fixed=$fixed"
"""


def _push_downloader_and_repair(rig) -> tuple[bool, str]:
    """Push the latest miner-downloader.sh and repair any gzipped-blob
    miner binaries on the rig. Triggered after SSH bootstrap so freshly-
    added rigs get the fix even if their image predated the downloader's
    magic-byte detection.
    """
    pool = get_pool()
    from pathlib import Path

    msgs = []

    # 1. Push the latest miner-downloader.sh from this repo to the rig.
    src = Path(__file__).resolve().parent.parent / "worker" / "miner-downloader.sh"
    if src.is_file():
        try:
            pool.upload_string(rig, src.read_text(encoding="utf-8"), "/opt/mfarm/miner-downloader.sh")
            pool.exec(rig, "sudo chmod +x /opt/mfarm/miner-downloader.sh", timeout=5)
            msgs.append("downloader updated")
        except Exception as e:
            msgs.append(f"downloader push failed: {e}")
    else:
        msgs.append(f"downloader source not found at {src}")

    # 2. Walk /opt/mfarm/miners/ and fix any gzipped-blob binaries in place.
    try:
        out, _, rc = pool.exec(rig, _REPAIR_MINERS_SCRIPT, timeout=60)
        msgs.append(out.strip().splitlines()[-1] if out.strip() else "repair: no output")
    except Exception as e:
        msgs.append(f"repair failed: {e}")
        return False, "; ".join(msgs)

    # 3. Restart mfarm-agent so the watchdog tries the fixed binary.
    try:
        pool.exec(rig, "sudo systemctl restart mfarm-agent", timeout=15)
        msgs.append("mfarm-agent restarted")
    except Exception as e:
        msgs.append(f"agent restart failed: {e}")

    return True, "; ".join(msgs)


@router.post("/rigs/{name}/push-key")
async def push_ssh_key(name: str):
    """Deploy the dashboard's SSH public key to root@<rig> using the default
    miner+sudo path. Used as a one-click bootstrap for freshly-flashed rigs
    where root SSH key auth isn't set up yet."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    pubkey = _find_dashboard_pubkey()
    if not pubkey:
        raise HTTPException(
            500,
            "No dashboard SSH public key found. Looked in ~/.ssh/id_ed25519.pub, "
            "~/.ssh/id_ecdsa.pub, ~/.ssh/id_rsa.pub, /etc/mfarm/dashboard_id_ed25519.pub. "
            "Generate one with `ssh-keygen -t ed25519` and retry."
        )
    loop = asyncio.get_event_loop()
    success, message = await loop.run_in_executor(
        _executor,
        lambda: _bootstrap_root_ssh(rig.host, rig.ssh_port, pubkey),
    )
    if not success:
        raise HTTPException(500, message)
    # Drop any cached SSH connection to this rig so subsequent ops use the
    # newly-authorized key path.
    try:
        pool = get_pool()
        with pool._lock:
            pool._clients.pop(rig.name, None)
    except Exception:
        pass
    # Now that we have root SSH, push the latest miner-downloader.sh and
    # repair any gzipped-blob miner binaries the rig was flashed with. This
    # turns "rig added → flight sheet apply → miner won't start" into
    # "rig added → just works" without operator intervention.
    repair_ok, repair_msg = await loop.run_in_executor(
        _executor, lambda: _push_downloader_and_repair(rig),
    )
    return {
        "status": "pushed",
        "message": message,
        "repair": repair_msg,
        "repair_ok": repair_ok,
    }


def _rig_to_dict(r: Rig) -> dict:
    return {
        "name": r.name, "host": r.host, "ssh_port": r.ssh_port,
        "ssh_user": r.ssh_user, "group": r.group_name,
        "flight_sheet": r.flight_sheet_name, "oc_profile": r.oc_profile_name,
        "agent_version": r.agent_version, "gpu_list": r.gpu_names,
        "cpu_model": r.cpu_model, "os_info": r.os_info, "notes": r.notes,
    }


@router.get("/rigs/{name}/history")
def get_rig_history(name: str, hours: int = 24):
    """Get hashrate/power history for a rig over the past N hours."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")
    rows = db.execute(
        "SELECT timestamp, hashrate, power_draw, accepted, rejected "
        "FROM rig_snapshots WHERE rig_id = ? AND timestamp >= datetime('now', ?) "
        "ORDER BY timestamp ASC",
        (rig.id, f"-{hours} hours"),
    ).fetchall()
    return [
        {"t": r[0], "hr": r[1], "power": r[2], "acc": r[3], "rej": r[4]}
        for r in rows
    ]


# ── Flight Sheets ────────────────────────────────────────────────────

class FlightSheetCreate(BaseModel):
    name: str
    coin: str
    algo: str
    miner: str
    pool_url: str
    wallet: str
    worker_template: str = "%HOSTNAME%"
    password: str = "x"
    pool_url2: str | None = None
    extra_args: str = ""
    is_solo: bool = False
    solo_rpc_user: str | None = None
    solo_rpc_pass: str | None = None
    coinbase_addr: str | None = None


@router.get("/flightsheets")
def get_flightsheets():
    db = get_db()
    return [_fs_to_dict(fs) for fs in FlightSheet.get_all(db)]


@router.post("/flightsheets")
def create_flightsheet(data: FlightSheetCreate):
    db = get_db()
    if FlightSheet.get_by_name(db, data.name):
        raise HTTPException(400, f"Flight sheet '{data.name}' already exists")
    fs = FlightSheet(
        name=data.name, coin=data.coin.upper(), algo=data.algo, miner=data.miner,
        pool_url=data.pool_url, pool_url2=data.pool_url2, wallet=data.wallet,
        worker_template=data.worker_template, password=data.password,
        extra_args=data.extra_args, is_solo=1 if data.is_solo else 0,
        solo_rpc_user=data.solo_rpc_user, solo_rpc_pass=data.solo_rpc_pass,
        coinbase_addr=data.coinbase_addr,
    )
    fs.save(db)
    return _fs_to_dict(FlightSheet.get_by_name(db, data.name))


@router.delete("/flightsheets/{name}")
def delete_flightsheet(name: str):
    db = get_db()
    fs = FlightSheet.get_by_name(db, name)
    if not fs:
        raise HTTPException(404)
    fs.delete(db)
    return {"status": "deleted"}


@router.post("/flightsheets/{fs_name}/apply/{target}")
async def apply_flightsheet(fs_name: str, target: str):
    from mfarm.targets import resolve_targets
    db = get_db()
    fs = FlightSheet.get_by_name(db, fs_name)
    if not fs:
        raise HTTPException(404, f"Flight sheet '{fs_name}' not found")

    rigs = resolve_targets(db, target)
    pool = get_pool()
    loop = asyncio.get_event_loop()
    results = {}

    for rig in rigs:
        try:
            rig.flight_sheet_id = fs.id
            rig.save(db)

            from mfarm.miners.registry import get_miner
            hostname = rig.name
            worker = fs.worker_template.replace("%HOSTNAME%", hostname).replace("%RIGNAME%", rig.name)
            miner_def = get_miner(fs.miner)
            api_port = miner_def.default_api_port if miner_def else 4068

            # Read current config, merge
            stdout, _, rc = await loop.run_in_executor(
                _executor, lambda r=rig: pool.exec(r, "cat /etc/mfarm/config.json", timeout=5)
            )
            config = json.loads(stdout) if rc == 0 and stdout.strip() else {"agent": {"version": "0.1.0"}, "miner_paths": {}, "api_ports": {}}

            config["flight_sheet"] = {
                "name": fs.name, "coin": fs.coin, "algo": fs.algo,
                "miner": fs.miner, "miner_version": fs.miner_version,
                "pool_url": fs.pool_url, "pool_url2": fs.pool_url2,
                "wallet": fs.wallet, "worker": worker, "password": fs.password,
                "extra_args": fs.extra_args, "is_solo": bool(fs.is_solo),
                "solo_rpc_user": fs.solo_rpc_user, "solo_rpc_pass": fs.solo_rpc_pass,
                "coinbase_addr": fs.coinbase_addr,
            }

            config_json = json.dumps(config, indent=2)

            def _upload_config(r=rig, c=config_json):
                try:
                    pool.upload_string(r, c, "/etc/mfarm/config.json")
                except (PermissionError, OSError, IOError) as upload_err:
                    # paramiko's SFTP raises IOError/OSError (NOT PermissionError)
                    # for both (a) chattr +i immutable files, and (b) files owned
                    # by another user. Older MeowOS images shipped with chattr +i,
                    # and newer ones may have config.json owned by miner user
                    # rather than root. Strip immutable + fix ownership, then retry.
                    pool.exec(
                        r,
                        "sudo chattr -i /etc/mfarm/config.json 2>/dev/null || chattr -i /etc/mfarm/config.json 2>/dev/null; "
                        "sudo chown $(whoami):$(whoami) /etc/mfarm/config.json 2>/dev/null || true",
                        timeout=5,
                    )
                    pool.upload_string(r, c, "/etc/mfarm/config.json")

            async def _apply():
                await loop.run_in_executor(_executor, _upload_config)
                await loop.run_in_executor(
                    _executor,
                    lambda r=rig: pool.upload_string(r, "apply_config", "/var/run/mfarm/command")
                )

            try:
                await _apply()
                results[rig.name] = "applied"
            except paramiko.AuthenticationException:
                # SSH key isn't authorized for the configured user (typically
                # root on freshly-imaged rigs). Auto-bootstrap: SSH as miner
                # with the default MeowOS password and deploy our public key
                # to root via NOPASSWD sudo, then retry the original op once.
                pubkey = _find_dashboard_pubkey()
                if not pubkey:
                    results[rig.name] = (
                        f"error: SSH auth failed for '{rig.ssh_user}', and the "
                        f"dashboard has no public key to bootstrap with. Generate "
                        f"one with `ssh-keygen -t ed25519` and try again."
                    )
                    continue
                ok, msg = await loop.run_in_executor(
                    _executor,
                    lambda r=rig, k=pubkey: _bootstrap_root_ssh(r.host, r.ssh_port, k),
                )
                if not ok:
                    results[rig.name] = f"error: SSH auth failed; auto-bootstrap also failed: {msg}"
                    continue
                # Drop the failed cached connection.
                try:
                    with pool._lock:
                        pool._clients.pop(rig.name, None)
                except Exception:
                    pass
                # Hot-patch the miner-downloader and repair any gzipped-
                # blob binaries left over from older MeowOS images. Failure
                # here doesn't block the apply — if the rig already has
                # working binaries this is a no-op.
                await loop.run_in_executor(
                    _executor, lambda r=rig: _push_downloader_and_repair(r),
                )
                try:
                    await _apply()
                    results[rig.name] = "applied (after auto-bootstrap)"
                except Exception as e2:
                    results[rig.name] = f"error: bootstrap succeeded but retry failed: {e2}"
        except paramiko.AuthenticationException:
            # Auth failed at the initial cat /etc/mfarm/config.json read. Same
            # auto-bootstrap path as above, then re-run the whole flow once
            # so the read+upload succeed against the now-authorized key.
            pubkey = _find_dashboard_pubkey()
            if not pubkey:
                results[rig.name] = (
                    f"error: SSH auth failed for '{rig.ssh_user}', and the "
                    f"dashboard has no public key to bootstrap with. Generate "
                    f"one with `ssh-keygen -t ed25519` and try again."
                )
                continue
            ok, msg = await loop.run_in_executor(
                _executor,
                lambda r=rig, k=pubkey: _bootstrap_root_ssh(r.host, r.ssh_port, k),
            )
            if not ok:
                results[rig.name] = f"error: SSH auth failed; auto-bootstrap also failed: {msg}"
                continue
            results[rig.name] = (
                "bootstrap-only: SSH key deployed, but flight-sheet apply was not "
                "retried (initial config read failed before key was deployed). "
                "Click Apply again."
            )
        except Exception as e:
            results[rig.name] = f"error: {e}"

    return {"results": results}


def _fs_to_dict(fs: FlightSheet) -> dict:
    return {
        "name": fs.name, "coin": fs.coin, "algo": fs.algo, "miner": fs.miner,
        "pool_url": fs.pool_url, "pool_url2": fs.pool_url2, "wallet": fs.wallet,
        "worker_template": fs.worker_template, "password": fs.password,
        "extra_args": fs.extra_args, "is_solo": bool(fs.is_solo),
        "solo_rpc_user": fs.solo_rpc_user, "coinbase_addr": fs.coinbase_addr,
    }


# ── OC Profiles ──────────────────────────────────────────────────────

class OcProfileCreate(BaseModel):
    name: str
    core_offset: int | None = None
    mem_offset: int | None = None
    core_lock: int | None = None
    mem_lock: int | None = None
    power_limit: int | None = None
    fan_speed: int | None = None


@router.get("/oc-profiles")
def get_oc_profiles():
    db = get_db()
    return [_oc_to_dict(p) for p in OcProfile.get_all(db)]


@router.post("/oc-profiles")
def create_oc_profile(data: OcProfileCreate):
    db = get_db()
    if OcProfile.get_by_name(db, data.name):
        raise HTTPException(400, f"OC profile '{data.name}' already exists")
    p = OcProfile(name=data.name, core_offset=data.core_offset,
                  mem_offset=data.mem_offset, core_lock=data.core_lock,
                  mem_lock=data.mem_lock, power_limit=data.power_limit,
                  fan_speed=data.fan_speed)
    p.save(db)
    return _oc_to_dict(OcProfile.get_by_name(db, data.name))


class OcProfileUpdate(BaseModel):
    core_offset: int | None = None
    mem_offset: int | None = None
    core_lock: int | None = None
    mem_lock: int | None = None
    power_limit: int | None = None
    fan_speed: int | None = None
    notes: str | None = None


@router.put("/oc-profiles/{name}")
def update_oc_profile(name: str, data: OcProfileUpdate):
    db = get_db()
    p = OcProfile.get_by_name(db, name)
    if not p:
        raise HTTPException(404, f"OC profile '{name}' not found")
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(p, field, value)
    p.save(db)
    return _oc_to_dict(OcProfile.get_by_name(db, name))


@router.delete("/oc-profiles/{name}")
def delete_oc_profile(name: str):
    db = get_db()
    p = OcProfile.get_by_name(db, name)
    if not p:
        raise HTTPException(404)
    p.delete(db)
    return {"status": "deleted"}


@router.post("/oc-profiles/{oc_name}/apply/{target}")
async def apply_oc_profile(oc_name: str, target: str):
    """Apply OC profile to rig(s) and deploy a persistent boot script."""
    from mfarm.targets import resolve_targets
    db = get_db()
    profile = OcProfile.get_by_name(db, oc_name)
    if not profile:
        raise HTTPException(404, f"OC profile '{oc_name}' not found")

    rigs = resolve_targets(db, target)
    pool = get_pool()
    loop = asyncio.get_event_loop()
    results = {}

    for rig in rigs:
        try:
            rig.oc_profile_id = profile.id
            rig.save(db)

            # Build the OC apply script that persists through reboots
            oc_script = '#!/bin/bash\nnvidia-smi -pm 1 > /dev/null 2>&1\n'
            oc_script += '# Try X for nvidia-settings, fall back gracefully\n'
            oc_script += 'if ! pgrep -x Xorg > /dev/null; then\n'
            oc_script += '  nohup Xorg :0 -config /etc/X11/xorg.conf > /dev/null 2>&1 &\n  sleep 3\nfi\nexport DISPLAY=:0\n'
            oc_script += 'GPU_COUNT=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l)\n'
            oc_script += 'for i in $(seq 0 $((GPU_COUNT-1))); do\n'
            if profile.core_offset is not None:
                oc_script += f'  nvidia-settings -a "[gpu:$i]/GPUGraphicsClockOffsetAllPerformanceLevels={profile.core_offset}" > /dev/null 2>&1 || true\n'
            if profile.mem_offset is not None:
                oc_script += f'  nvidia-settings -a "[gpu:$i]/GPUMemoryTransferRateOffsetAllPerformanceLevels={profile.mem_offset}" > /dev/null 2>&1 || true\n'
            if profile.core_lock is not None:
                oc_script += f'  nvidia-smi -i $i -lgc {profile.core_lock},{profile.core_lock} > /dev/null 2>&1\n'
            if profile.mem_lock is not None:
                oc_script += f'  nvidia-smi -i $i -lmc {profile.mem_lock},{profile.mem_lock} > /dev/null 2>&1\n'
            if profile.power_limit is not None:
                oc_script += f'  nvidia-smi -i $i -pl {profile.power_limit} > /dev/null 2>&1\n'
            if profile.fan_speed is not None:
                oc_script += f'  nvidia-settings -a "[gpu:$i]/GPUFanControlState=1" > /dev/null 2>&1\n'
                oc_script += f'  nvidia-settings -a "[fan:$i]/GPUTargetFanSpeed={profile.fan_speed}" > /dev/null 2>&1\n'
            oc_script += 'done\n'
            oc_script += 'nvidia-smi -pm 1 > /dev/null 2>&1\n'
            oc_script += f'echo "OC {oc_name} applied at $(date)" >> /var/log/mfarm/oc.log\n'

            # Upload OC script to temp, then sudo move it
            await loop.run_in_executor(
                _executor, lambda r=rig, s=oc_script: pool.upload_string(r, s, "/tmp/apply-oc.sh")
            )
            await loop.run_in_executor(
                _executor, lambda r=rig: pool.exec(r, "sudo cp /tmp/apply-oc.sh /opt/mfarm/apply-oc.sh && sudo chmod +x /opt/mfarm/apply-oc.sh", timeout=5)
            )

            # Create systemd service for boot persistence
            svc = '[Unit]\nDescription=MeowFarm GPU Overclock\nAfter=multi-user.target\nWants=mfarm-agent.service\n\n[Service]\nType=oneshot\nExecStart=/opt/mfarm/apply-oc.sh\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n'
            await loop.run_in_executor(
                _executor, lambda r=rig, s=svc: pool.upload_string(r, s, "/tmp/mfarm-oc.service")
            )
            await loop.run_in_executor(
                _executor, lambda r=rig: pool.exec(r, "sudo cp /tmp/mfarm-oc.service /etc/systemd/system/mfarm-oc.service && sudo systemctl daemon-reload && sudo systemctl enable mfarm-oc.service", timeout=10)
            )

            # Apply now
            await loop.run_in_executor(
                _executor, lambda r=rig: pool.exec(r, "sudo bash /opt/mfarm/apply-oc.sh", timeout=30)
            )

            results[rig.name] = "applied (persists on reboot)"
        except Exception as e:
            results[rig.name] = f"error: {e}"

    return {"results": results}


def _oc_to_dict(p: OcProfile) -> dict:
    return {
        "name": p.name, "core_offset": p.core_offset, "mem_offset": p.mem_offset,
        "core_lock": p.core_lock, "mem_lock": p.mem_lock,
        "power_limit": p.power_limit, "fan_speed": p.fan_speed,
        "per_gpu": p.per_gpu,
    }


# ── Miner Console ────────────────────────────────────────────────────

@router.get("/rigs/{name}/miner-log/{miner_type}")
async def get_miner_log(name: str, miner_type: str, lines: int = 80):
    """Get the last N lines of a miner log.

    Log file routing matches what the agent (mfarm-agent.py) actually writes:
      - GPU miner stdout → /var/log/mfarm/miner.log  (MINER_LOG_PATH)
      - CPU miner stdout → /var/log/mfarm/cpu-miner.log  (NOT xmrig.log)

    Special case: when the rig is CPU-only (primary flight_sheet miner == xmrig),
    the agent runs XMRig as the "GPU" process and writes to miner.log. The CPU
    button on a CPU-only rig should therefore read miner.log, not the empty
    cpu-miner.log. Otherwise the user sees "GPU shows CPU output" because the
    only mining log on the rig is XMRig's, in miner.log.
    """
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)

    # Detect what the rig is actually running so log routing matches.
    fs = FlightSheet.get_by_id(db, rig.flight_sheet_id) if rig.flight_sheet_id else None
    primary_is_cpu = fs and fs.miner and fs.miner.lower() in ("xmrig", "cpuminer", "cpuminer-opt")

    if miner_type == "cpu":
        # If primary fs is a CPU miner, its output is in miner.log (since the
        # agent treats the primary fs as the "GPU" process). Else use the
        # secondary cpu-miner.log path.
        log_file = "/var/log/mfarm/miner.log" if primary_is_cpu else "/var/log/mfarm/cpu-miner.log"
    else:
        # GPU button. If primary fs is CPU-only, there is no GPU miner — return
        # a friendly message instead of empty miner.log (which would be CPU output).
        if primary_is_cpu:
            return {
                "log": f"(no GPU miner running — primary flight sheet '{fs.name}' uses {fs.miner})",
                "file": None,
            }
        log_file = "/var/log/mfarm/miner.log"

    pool = get_pool()
    loop = asyncio.get_event_loop()
    try:
        stdout, _, _ = await loop.run_in_executor(
            _executor, lambda: pool.exec(rig, f"tail -n {lines} {log_file} 2>/dev/null", timeout=5)
        )
        return {"log": stdout, "file": log_file}
    except Exception as e:
        return {"log": f"Error: {e}", "file": log_file}


@router.post("/rigs/{name}/miner-api/{miner_type}")
async def query_miner_api(name: str, miner_type: str, body: dict):
    """Query miner API directly on the rig. For XMRig: HTTP API. For ccminer: TCP API."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    command = body.get("command", "summary")
    pool = get_pool()
    loop = asyncio.get_event_loop()

    if miner_type == "cpu":
        # XMRig HTTP API on port 44445
        endpoint = {
            "hashrate": "/2/backends",
            "summary": "/1/summary",
            "config": "/1/config",
            "results": "/1/summary",
        }.get(command, f"/1/{command}")
        cmd = f"curl -s http://127.0.0.1:44445{endpoint} 2>/dev/null"
    else:
        # ccminer TCP API on port 4068
        cmd = f"echo '{command}' | nc -w 2 127.0.0.1 4068 2>/dev/null"

    try:
        stdout, _, _ = await loop.run_in_executor(
            _executor, lambda: pool.exec(rig, cmd, timeout=10)
        )
        # Try to parse as JSON for pretty display
        try:
            import json as _json
            data = _json.loads(stdout.replace('\0', ''))
            return {"result": data, "raw": False}
        except Exception:
            return {"result": stdout.replace('\0', ''), "raw": True}
    except Exception as e:
        return {"result": str(e), "raw": True}


# ── Phone Home ───────────────────────────────────────────────────────

@router.post("/phonehome")
async def phonehome(body: dict):
    """Receive phone-home from a MeowOS rig."""
    from mfarm.web.app import _handle_phonehome, _discovered_rigs
    _handle_phonehome(body)
    return {"status": "ok"}


@router.get("/discovered")
def get_discovered():
    """List rigs that have phoned home but aren't claimed by an existing DB
    entry or user-dismissed.

    A discovered rig is suppressed when ANY of these match an existing rig:
      1. MAC is in the persistent _dismissed_macs set (manual dismiss, or
         auto-dismissed because of IP/hostname match below).
      2. Phoned-home IP equals rig.host.
      3. Phoned-home hostname equals rig.name (case-insensitive).

    Cases 2 and 3 also auto-promote the MAC into _dismissed_macs so the rig
    stays suppressed if its IP later changes (DHCP renew) or its hostname
    drifts. Without this, the popup re-fires every poll for already-claimed
    rigs whose IP changed.
    """
    from mfarm.web.app import _discovered_rigs, _dismissed_macs, _save_dismissed_macs
    import time
    db = get_db()
    rigs = Rig.get_all(db)
    existing_hosts = {r.host for r in rigs}
    existing_names = {r.name.lower() for r in rigs if r.name}
    result = []
    newly_dismissed = False
    for mac, info in _discovered_rigs.items():
        if mac in _dismissed_macs:
            continue
        ip = info.get("ip", "")
        hostname = (info.get("hostname") or "").lower()
        if ip in existing_hosts or (hostname and hostname in existing_names):
            _dismissed_macs.add(mac)
            newly_dismissed = True
            continue
        info_copy = dict(info)
        info_copy["age_secs"] = round(time.time() - info.get("last_seen", 0))
        result.append(info_copy)
    if newly_dismissed:
        _save_dismissed_macs(_dismissed_macs)
    return result


@router.post("/discovered/{mac}/dismiss")
def dismiss_discovered(mac: str):
    """Mark a discovered rig's MAC as dismissed so it stops showing in
    the popup. Persists across server restarts."""
    from mfarm.web.app import _dismissed_macs, _save_dismissed_macs
    mac = mac.lower()
    _dismissed_macs.add(mac)
    _save_dismissed_macs(_dismissed_macs)
    return {"status": "dismissed", "mac": mac}


# ── MAC Address Lookup ───────────────────────────────────────────────

@router.post("/find-by-mac")
async def find_by_mac(body: dict):
    """Scan the local subnet ARP table for a MAC address."""
    import subprocess
    import re

    mac = body.get("mac", "").strip().lower()
    if not mac:
        raise HTTPException(400, "No MAC address provided")

    # Normalize MAC to dash-separated (Windows ARP format)
    mac_dashed = mac.replace(":", "-").lower()

    loop = asyncio.get_event_loop()

    def _scan():
        # Ping sweep the subnet first to populate ARP table
        # Find our local subnet
        try:
            result = subprocess.run(
                ["powershell", "-Command",
                 "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -like '192.168.*'}).IPAddress"],
                capture_output=True, text=True, timeout=5)
            local_ip = result.stdout.strip().split('\n')[0].strip()
        except Exception:
            local_ip = "192.168.68.78"

        subnet = ".".join(local_ip.split(".")[:3])

        # Quick ping sweep to populate ARP
        for i in range(1, 255):
            subprocess.Popen(
                ["ping", "-n", "1", "-w", "200", f"{subnet}.{i}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

        import time
        time.sleep(5)

        # Read ARP table
        result = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10)
        for line in result.stdout.split("\n"):
            line_lower = line.lower().strip()
            if mac_dashed in line_lower:
                # Extract IP from line like "  192.168.68.33        f4-b5-20-02-56-45     dynamic"
                match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                if match:
                    return match.group(1)
        return None

    ip = await loop.run_in_executor(_executor, _scan)
    return {"ip": ip, "mac": mac}


# ── Groups ───────────────────────────────────────────────────────────

@router.get("/groups")
def get_groups():
    db = get_db()
    groups = Group.get_all(db)
    result = []
    for g in groups:
        rigs = Rig.get_all(db, group_name=g.name)
        result.append({"name": g.name, "notes": g.notes, "rig_count": len(rigs)})
    return result


# ── Miners ───────────────────────────────────────────────────────────

@router.get("/miners")
def get_miners():
    return [{"name": m.name, "display_name": m.display_name,
             "gpu_type": m.gpu_type, "algos": m.supported_algos,
             "supports_solo": m.supports_solo}
            for m in list_miners()]


@router.post("/rigs/{name}/update-miners")
async def update_miners(name: str):
    """Update all miner binaries on a rig.

    Step 1: push the latest miner-downloader.sh from this checkout so the
    rig has the magic-byte-detecting version (rigs flashed before that fix
    have a downloader that silently leaves .tar.gz blobs at /opt/mfarm/
    miners/xmrig and triggers Exec format error in the agent's restart loop).

    Step 2: scan /opt/mfarm/miners/ and re-extract any gzipped blob in
    place, then restart mfarm-agent. Same logic the bootstrap path uses,
    but reachable via the existing 'Update Miners' button.

    Step 3: run the (now-current) downloader to pull any newer releases.
    """
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")
    pool = get_pool()
    loop = asyncio.get_event_loop()
    parts = []
    try:
        # Steps 1+2 (push fresh downloader + repair gzipped blobs + restart)
        ok, msg = await loop.run_in_executor(
            _executor, lambda r=rig: _push_downloader_and_repair(r),
        )
        parts.append(f"prep: {msg}")
        if not ok:
            return {"status": "error", "error": "; ".join(parts)}
        # Step 3 (run the now-current downloader to pull updates)
        out, err, rc = await loop.run_in_executor(
            _executor,
            lambda r=rig: pool.exec(r, "sudo bash /opt/mfarm/miner-downloader.sh all", timeout=300),
        )
        parts.append(f"download rc={rc}")
        return {"status": "updated", "output": out, "log": "\n".join(parts)}
    except Exception as e:
        parts.append(f"exception: {e}")
        return {"status": "error", "error": "; ".join(parts)}


@router.get("/version")
def get_version():
    from mfarm import __version__
    return {"version": __version__}
