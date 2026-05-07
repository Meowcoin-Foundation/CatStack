"""REST API routes for MFarm web dashboard."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import shlex
import subprocess
import time as _time
from concurrent.futures import ThreadPoolExecutor

import paramiko
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger(__name__)

from mfarm.db.connection import get_db
from mfarm.db.models import Rig, FlightSheet, OcProfile, Group
from mfarm.ssh.pool import get_pool
from mfarm.miners.registry import list_miners, get_miner, parse_algo_output
from mfarm.web import agent_state

# Stats are considered "fresh" while the agent has pushed within this many
# seconds. Beyond it, fall back to the SSH-pull legacy path. 30s gives an
# agent at the default 2s stats_interval ~15 attempts to recover from a
# transient outage before the dashboard goes back to SSH.
PUSH_STATS_TTL_SECS = 30

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
    _hook_router_apply(rig.name)
    _hook_rig_hostname_sync(rig.name)

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
    if data.host is not None or data.name is not None:
        _hook_router_apply(rig.name)
    if data.name is not None:
        _hook_rig_hostname_sync(rig.name)
    return _rig_to_dict(rig)


@router.delete("/rigs/{name}")
def delete_rig(name: str):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")
    rig.delete(db)
    _hook_router_remove(name)
    return {"status": "deleted"}


@router.post("/rigs/{name}/agent-token")
def rotate_agent_token(name: str):
    """Generate a fresh agent token for this rig and store it.

    Returns `{"token": "<plaintext>"}`. The operator must immediately
    deploy this token into the rig's `/etc/mfarm/config.json` (field
    `agent_token`) — once the dashboard navigates away, there's no
    "show again" flow; rotating issues a brand-new token and orphans
    the old one.

    Calling this on a rig that already has a token is fine and
    intended: the rig's old token stops working as soon as the new one
    is committed, which is the right behavior on suspected compromise
    or when re-claiming a recovered rig."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")
    token = secrets.token_urlsafe(32)
    Rig.set_agent_token(db, rig.id, token)
    return {"token": token}


@router.get("/rigs/{name}/agent-token-status")
def agent_token_status(name: str):
    """Whether this rig has an agent token issued. Does NOT reveal the
    token itself — that's only returned at issue time. The dashboard
    uses this to render an "Issue token" vs "Rotate token" button."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")
    return {"issued": rig.agent_token is not None}


@router.post("/rigs/{name}/sync-hostname")
async def sync_rig_hostname(name: str):
    """On-demand hostname sync — useful for rigs claimed before the
    auto-sync hook existed. Synchronous; returns the result."""
    db = get_db()
    if not Rig.get_by_name(db, name):
        raise HTTPException(404, f"rig '{name}' not found")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: _apply_rig_hostname(name))


@router.post("/rigs/sync-hostnames")
async def sync_all_hostnames():
    """Bulk hostname sync across the fleet. Returns per-rig result.
    Skips invalid names (with underscores etc.) silently with action='skipped'."""
    db = get_db()
    rigs = Rig.get_all(db)
    loop = asyncio.get_event_loop()
    futures = [
        (r.name, loop.run_in_executor(_executor, lambda n=r.name: _apply_rig_hostname(n)))
        for r in rigs
    ]
    results = {}
    for name, fut in futures:
        try:
            results[name] = await asyncio.wait_for(fut, timeout=30)
        except Exception as e:
            results[name] = {"ok": False, "action": "error", "message": f"timeout/error: {e}"}
    return {"results": results}


def _hook_router_apply(rig_name: str) -> None:
    """Fire-and-forget: apply current backend's port-forward rule for rig.

    Runs in the existing thread pool so the synchronous rig CRUD handlers
    return immediately. If the rig isn't a Vast host (no port range) we
    proactively remove any stale rule under that name."""
    def _do():
        try:
            from mfarm.router.base import ForwardRule
            from mfarm.router.store import current_backend
            db = get_db()
            rig = Rig.get_by_name(db, rig_name)
            if not rig:
                return
            backend = current_backend(db)
            rng = _read_rig_port_range(rig)
            if rng is None:
                backend.remove_rule(rig_name)
                return
            rule = ForwardRule(
                rig_name=rig_name, internal_ip=rig.host,
                port_lo=rng[0], port_hi=rng[1],
            )
            res = backend.apply_rule(rule)
            if not res.ok:
                log.warning("router apply for %s: %s", rig_name, "; ".join(res.messages))
        except Exception as e:
            log.warning("router hook apply failed for %s: %s", rig_name, e)
    _executor.submit(_do)


def _hook_router_remove(rig_name: str) -> None:
    def _do():
        try:
            from mfarm.router.store import current_backend
            db = get_db()
            backend = current_backend(db)
            backend.remove_rule(rig_name)
        except Exception as e:
            log.warning("router hook remove failed for %s: %s", rig_name, e)
    _executor.submit(_do)


# Valid hostname pattern per RFC 1123: alphanumeric + hyphens, doesn't start
# with hyphen, max 63 chars. Rig names with underscores or spaces (e.g.
# "Octo_Top") get skipped — Linux accepts them but they break DNS / Vast
# portal display.
_HOSTNAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$')


def _apply_rig_hostname(rig_name: str) -> dict:
    """Synchronously rename the rig's system hostname to match its CatStack
    name. Returns {ok, action, message}. Used by both the on-demand
    /sync-hostname endpoint and the fire-and-forget post-CRUD hook.

    Skips when the rig name isn't a valid RFC 1123 hostname (e.g. contains
    underscores or spaces) — Linux would accept it but breaks DNS / Vast
    portal display."""
    if not _HOSTNAME_RE.match(rig_name):
        return {"ok": False, "action": "skipped", "message": f"'{rig_name}' is not a valid RFC 1123 hostname"}
    db = get_db()
    rig = Rig.get_by_name(db, rig_name)
    if not rig:
        return {"ok": False, "action": "error", "message": f"rig '{rig_name}' not found"}
    try:
        pool = get_pool()
        cmd = (
            f"current=$(hostname); "
            f"if [ \"$current\" != '{rig_name}' ]; then "
            f"  hostnamectl set-hostname '{rig_name}' && "
            f"  sed -i 's/^127\\.0\\.1\\.1.*/127.0.1.1\\t{rig_name}/' /etc/hosts; "
            f"  grep -q '^127\\.0\\.1\\.1' /etc/hosts || echo -e '127.0.1.1\\t{rig_name}' >> /etc/hosts; "
            f"  if systemctl is-active --quiet vastai 2>/dev/null; then "
            f"    systemctl restart vastai vast_metrics 2>/dev/null || true; "
            f"  fi; "
            f"  echo \"renamed: $current -> {rig_name}\"; "
            f"else "
            f"  echo \"already {rig_name}\"; "
            f"fi"
        )
        out, _, rc = pool.exec(rig, cmd, timeout=20)
        if rc != 0:
            return {"ok": False, "action": "error", "message": f"SSH exit {rc}: {out.strip()}"}
        out = out.strip()
        if out.startswith("renamed:"):
            return {"ok": True, "action": "renamed", "message": out}
        return {"ok": True, "action": "noop", "message": out}
    except Exception as e:
        return {"ok": False, "action": "error", "message": f"{type(e).__name__}: {e}"}


def _hook_rig_hostname_sync(rig_name: str) -> None:
    """Fire-and-forget wrapper around _apply_rig_hostname for use in CRUD
    hooks where we don't want to block the response."""
    def _do():
        res = _apply_rig_hostname(rig_name)
        if res["ok"] and res["action"] == "renamed":
            log.info("hostname sync %s: %s", rig_name, res["message"])
        elif not res["ok"]:
            log.warning("hostname sync failed for %s: %s", rig_name, res["message"])
    _executor.submit(_do)


@router.get("/rigs/{name}/stats")
async def get_rig_stats(name: str):
    """Fetch fresh stats from a rig on demand.

    Cache-first path: if the rig's agent has pushed within
    PUSH_STATS_TTL_SECS, return that — a dict lookup, no SSH.

    Fallback: legacy SSH-cat of /var/run/mfarm/stats.json. Stays in place
    until every rig is on the push transport (no token issued ⇒ no push ⇒
    fallback wins). Once the fleet is fully migrated, drop the SSH branch
    and the `pool` import dependence here goes with it.
    """
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)

    s = agent_state.get(rig.id)
    if s.last_stats is not None and agent_state.stats_age(rig.id) < PUSH_STATS_TTL_SECS:
        return s.last_stats

    # Skip SSH fallback when the rig has no host on file. Without this guard,
    # paramiko spins for the full 10s watchdog window on an empty hostname
    # and the dashboard endpoint hangs. A blank host means we lost track of
    # this rig's IP — phonehome will repopulate it when the rig comes back.
    if not rig.host:
        return {}

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
    """Reboot a rig.

    Enqueue-first: if the rig's agent has pushed recently, drop a `reboot`
    command on its long-poll queue. The agent will execute, the rig goes
    down, no result comes back — that's the expected signal for a reboot
    command and not an error.

    Fallback: legacy SSH `reboot`. Used for rigs that don't have a token
    yet, or whose agent has gone silent (>PUSH_STATS_TTL_SECS since last
    push) — in the latter case the rig may be wedged anyway and SSH is
    our only remaining lever.
    """
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)

    if agent_state.stats_age(rig.id) < PUSH_STATS_TTL_SECS:
        agent_state.enqueue(rig.id, "reboot")
        return {"status": "rebooting", "via": "agent"}

    pool = get_pool()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_executor, lambda: pool.exec(rig, "reboot", timeout=5))
    except Exception:
        pass
    return {"status": "rebooting", "via": "ssh"}


@router.post("/rigs/{name}/exec")
async def exec_on_rig(name: str, body: dict):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    command = body.get("command", "")
    if not command:
        raise HTTPException(400, "No command provided")

    # Push path: enqueue an `exec` command on the agent's long-poll and
    # block on the result. The agent runs `bash -lc <command>` itself so
    # /etc/profile.d aliases resolve identically to the SSH path. We give
    # the wait 35s — 5s slack over the agent's own 30s subprocess timeout
    # so a TimeoutExpired translates into rc=124 from the agent rather
    # than a wait_for_result timeout (the latter would silently fall back
    # to SSH and re-run the command, bad for non-idempotent ops).
    if agent_state.stats_age(rig.id) < PUSH_STATS_TTL_SECS:
        result = await agent_state.enqueue_and_wait(
            rig.id, "exec", {"cmd": command, "timeout": 30}, timeout=35,
        )
        if result is not None:
            return {
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "exit_code": result.get("rc", -1),
                "via": "agent",
            }
        # Wait timed out — agent went silent mid-command. Fall through to
        # SSH for this single call. The next request will re-evaluate
        # stats_age; if the agent is healthy the cache flips back over.

    pool = get_pool()
    loop = asyncio.get_event_loop()
    # Same shell wrap on the SSH side — paramiko exec_command skips
    # /etc/profile by default; without the wrap, miner-attach.sh aliases
    # like `miner restart` aren't in scope.
    wrapped = f"bash -lc {shlex.quote(command)}"
    try:
        stdout, stderr, rc = await loop.run_in_executor(
            _executor, lambda: pool.exec(rig, wrapped, timeout=30)
        )
        return {"stdout": stdout, "stderr": stderr, "exit_code": rc, "via": "ssh"}
    except Exception as e:
        return {"error": str(e), "via": "ssh"}


@router.post("/rigs/{name}/restart-miner")
async def restart_miner(name: str):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)

    # Fire-and-forget: don't wait for the agent's result — `restart_miner`
    # gives the same end state regardless and the dashboard will see the
    # miner come back up via the next stats push.
    if agent_state.stats_age(rig.id) < PUSH_STATS_TTL_SECS:
        agent_state.enqueue(rig.id, "restart_miner")
        return {"status": "restart_sent", "via": "agent"}

    pool = get_pool()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            _executor,
            lambda: pool.upload_string(rig, "restart_miner", "/var/run/mfarm/command")
        )
        return {"status": "restart_sent", "via": "ssh"}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/rigs/{name}/stop-miner")
async def stop_miner(name: str):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)

    if agent_state.stats_age(rig.id) < PUSH_STATS_TTL_SECS:
        agent_state.enqueue(rig.id, "stop_miner")
        return {"status": "stop_sent", "via": "agent"}

    pool = get_pool()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            _executor,
            lambda: pool.upload_string(rig, "stop_miner", "/var/run/mfarm/command")
        )
        return {"status": "stop_sent", "via": "ssh"}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/rigs/{name}/external-fan")
async def set_external_fan(name: str, body: dict):
    """Set an Octominer chassis fan PWM via the AVR USB MCU.

    body = {"index": int, "pct": 0..100}. Setting persists until the rig
    reboots or the agent's next manual override. Push path goes through
    the long-poll queue and waits ~10s for the agent to confirm. SSH
    fallback runs fan_controller_cli directly when the agent's offline.
    """
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    try:
        index = int(body.get("index"))
        pct = int(body.get("pct"))
    except (TypeError, ValueError):
        raise HTTPException(400, "index and pct must be integers")
    if not 0 <= pct <= 100:
        raise HTTPException(400, "pct must be 0..100")

    if agent_state.stats_age(rig.id) < PUSH_STATS_TTL_SECS:
        result = await agent_state.enqueue_and_wait(
            rig.id, "set_external_fan", {"index": index, "pct": pct}, timeout=10,
        )
        if result is not None:
            return {
                "ok": result.get("rc", -1) == 0,
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "via": "agent",
            }

    pool = get_pool()
    loop = asyncio.get_event_loop()
    pwm = max(0, min(255, round(255 * pct / 100)))
    cmd = f"/opt/mfarm/fan_controller_cli -f {index} -v {pwm}"
    try:
        stdout, stderr, rc = await loop.run_in_executor(
            _executor, lambda: pool.exec(rig, cmd, timeout=10)
        )
        return {"ok": rc == 0, "stdout": stdout, "stderr": stderr, "via": "ssh"}
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


# ── MAC address resolution ──────────────────────────────────────────
# Cached system ARP table + phone-home merge. Used by both /api/rigs
# (REST) and the WS push so the dashboard can show each rig's MAC next
# to its IP — useful for finding a rig after a DHCP rotation, and for
# diagnosing "rig screen looks fine but it's offline" (no MAC = no L2
# presence on any subnet we can reach).
_arp_cache: dict[str, str] = {}
_arp_cache_ts: float = 0.0
_ARP_CACHE_TTL = 30.0
_ARP_LINE_RE = re.compile(
    r"(\d{1,3}(?:\.\d{1,3}){3})"
    r"\D+"
    r"([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})"
)


def _refresh_arp_cache() -> dict[str, str]:
    """Read system ARP table; return {ip: mac}. 30s in-memory TTL.

    Parses both the Windows (`192.168.x.y  xx-xx-...`) and Linux
    (`? (192.168.x.y) at xx:xx:... [ether]`) output of `arp -a`. Failures
    return the last good cache rather than wiping it — better stale than
    blank.
    """
    global _arp_cache, _arp_cache_ts
    now = _time.time()
    if _arp_cache and now - _arp_cache_ts < _ARP_CACHE_TTL:
        return _arp_cache
    try:
        out = subprocess.run(
            ["arp", "-a"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return _arp_cache
    fresh: dict[str, str] = {}
    for line in out.splitlines():
        m = _ARP_LINE_RE.search(line)
        if m:
            fresh[m.group(1)] = m.group(2).replace("-", ":").lower()
    _arp_cache = fresh
    _arp_cache_ts = now
    return fresh


def _ip_to_mac_table() -> dict[str, str]:
    """Combined view: live ARP + phone-home history.

    Phone-home wins over ARP when both have an entry — the rig has told
    us its own MAC explicitly. ARP fills in for rigs that don't run
    mfarm-agent (HiveOS rigs) or that aren't currently reachable but were
    in cache before they went down.
    """
    table = dict(_refresh_arp_cache())
    try:
        from mfarm.web.app import _discovered_rigs
        for mac, info in _discovered_rigs.items():
            ip = info.get("ip")
            if ip:
                table[ip] = mac.lower()
    except Exception:
        pass
    return table


def _rig_to_dict(r: Rig) -> dict:
    return {
        "name": r.name, "host": r.host, "ssh_port": r.ssh_port,
        "ssh_user": r.ssh_user, "group": r.group_name,
        "flight_sheet": r.flight_sheet_name, "oc_profile": r.oc_profile_name,
        "agent_version": r.agent_version, "gpu_list": r.gpu_names,
        "cpu_model": r.cpu_model, "os_info": r.os_info, "notes": r.notes,
        # Prefer the MAC stored on the rig (populated by phonehome reconcile);
        # fall back to ARP only when we haven't seen a phonehome yet.
        "mac": r.mac or _ip_to_mac_table().get(r.host),
    }


@router.get("/rigs/{name}/history")
def get_rig_history(name: str, hours: int = 24):
    """Get hashrate/power history for a rig over the past N hours."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")
    rows = db.execute(
        "SELECT timestamp, hashrate, power_draw, accepted, rejected, algo "
        "FROM rig_snapshots WHERE rig_id = ? AND timestamp >= datetime('now', ?) "
        "ORDER BY timestamp ASC",
        (rig.id, f"-{hours} hours"),
    ).fetchall()
    return [
        {"t": r[0], "hr": r[1], "power": r[2], "acc": r[3], "rej": r[4], "algo": r[5]}
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


class FlightSheetUpdate(BaseModel):
    coin: str | None = None
    algo: str | None = None
    miner: str | None = None
    pool_url: str | None = None
    pool_url2: str | None = None
    wallet: str | None = None
    worker_template: str | None = None
    password: str | None = None
    extra_args: str | None = None
    is_solo: bool | None = None
    solo_rpc_user: str | None = None
    solo_rpc_pass: str | None = None
    coinbase_addr: str | None = None


@router.put("/flightsheets/{name}")
def update_flightsheet(name: str, data: FlightSheetUpdate):
    db = get_db()
    fs = FlightSheet.get_by_name(db, name)
    if not fs:
        raise HTTPException(404, f"Flight sheet '{name}' not found")
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if field == "is_solo":
            value = 1 if value else 0
        elif field == "coin" and value is not None:
            value = value.upper()
        setattr(fs, field, value)
    fs.save(db)
    return _fs_to_dict(FlightSheet.get_by_name(db, name))


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
                # Marker so the agent's Vast-host guard knows this flight
                # sheet was an explicit operator action (applied via the
                # dashboard) rather than a default baked into the image.
                # Vast hosts only auto-run GPU miners when this is set.
                "applied_via_dashboard": True,
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
    per_gpu: list[dict] | None = None


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
                  fan_speed=data.fan_speed,
                  per_gpu_overrides=json.dumps(data.per_gpu) if data.per_gpu else None)
    p.save(db)
    return _oc_to_dict(OcProfile.get_by_name(db, data.name))


class OcProfileUpdate(BaseModel):
    core_offset: int | None = None
    mem_offset: int | None = None
    core_lock: int | None = None
    mem_lock: int | None = None
    power_limit: int | None = None
    fan_speed: int | None = None
    per_gpu: list[dict] | None = None
    notes: str | None = None


@router.put("/oc-profiles/{name}")
def update_oc_profile(name: str, data: OcProfileUpdate):
    db = get_db()
    p = OcProfile.get_by_name(db, name)
    if not p:
        raise HTTPException(404, f"OC profile '{name}' not found")
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if field == "per_gpu":
            p.per_gpu_overrides = json.dumps(value) if value else None
        else:
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


def _build_oc_script(profile: OcProfile, label: str) -> str:
    """Render the persistent /opt/mfarm/apply-oc.sh body for `profile`.

    Per-GPU overrides (profile.per_gpu) are applied on top of the global
    settings: any field present on a per-GPU entry replaces the global for
    that GPU index; missing fields fall back to the global.
    """
    overrides: dict[int, dict] = {}
    if profile.per_gpu:
        for entry in profile.per_gpu:
            idx = entry.get("gpu")
            if isinstance(idx, int):
                overrides[idx] = entry

    # Every nvidia-* invocation appends to oc.log so silent failures (e.g.
    # `-lgc` rejecting an unsupported clock value, or persistence not yet
    # active on this GPU) become visible. We also `nvidia-smi -i $i -pm 1`
    # and `-rgc` per-GPU before locking — on consumer Ada cards a stale
    # prior lock or per-GPU persistence-off state silently no-ops the new
    # lock.
    def _emit(eff: dict, indent: str) -> str:
        out = ""
        if eff.get("core_offset") is not None:
            out += f'{indent}nvidia-settings -a "[gpu:$i]/GPUGraphicsClockOffsetAllPerformanceLevels={eff["core_offset"]}" >> "$LOG" 2>&1 || true\n'
        if eff.get("mem_offset") is not None:
            out += f'{indent}nvidia-settings -a "[gpu:$i]/GPUMemoryTransferRateOffsetAllPerformanceLevels={eff["mem_offset"]}" >> "$LOG" 2>&1 || true\n'
        if eff.get("core_lock") is not None:
            out += f'{indent}nvidia-smi -i $i -pm 1 >> "$LOG" 2>&1\n'
            out += f'{indent}nvidia-smi -i $i -rgc >> "$LOG" 2>&1\n'
            out += f'{indent}nvidia-smi -i $i -lgc {eff["core_lock"]},{eff["core_lock"]} >> "$LOG" 2>&1\n'
        if eff.get("mem_lock") is not None:
            out += f'{indent}nvidia-smi -i $i -pm 1 >> "$LOG" 2>&1\n'
            out += f'{indent}nvidia-smi -i $i -rmc >> "$LOG" 2>&1\n'
            out += f'{indent}nvidia-smi -i $i -lmc {eff["mem_lock"]},{eff["mem_lock"]} >> "$LOG" 2>&1\n'
        if eff.get("power_limit") is not None:
            out += f'{indent}nvidia-smi -i $i -pl {eff["power_limit"]} >> "$LOG" 2>&1\n'
        if eff.get("fan_speed") is not None:
            out += f'{indent}nvidia-settings -a "[gpu:$i]/GPUFanControlState=1" >> "$LOG" 2>&1\n'
            out += f'{indent}nvidia-settings -a "[fan:$i]/GPUTargetFanSpeed={eff["fan_speed"]}" >> "$LOG" 2>&1\n'
        return out

    globals_ = {
        "core_offset": profile.core_offset, "mem_offset": profile.mem_offset,
        "core_lock":   profile.core_lock,   "mem_lock":   profile.mem_lock,
        "power_limit": profile.power_limit, "fan_speed":  profile.fan_speed,
    }

    s = '#!/bin/bash\n'
    s += 'LOG=/var/log/mfarm/oc.log\n'
    s += 'mkdir -p /var/log/mfarm\n'
    s += f'echo "===== OC {label} applying at $(date) =====" >> "$LOG"\n'
    s += 'nvidia-smi -pm 1 >> "$LOG" 2>&1\n'
    s += '# Try X for nvidia-settings, fall back gracefully\n'
    s += 'if ! pgrep -x Xorg > /dev/null; then\n'
    s += '  nohup Xorg :0 -config /etc/X11/xorg.conf > /dev/null 2>&1 &\n  sleep 3\nfi\nexport DISPLAY=:0\n'
    s += 'GPU_COUNT=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l)\n'
    s += 'for i in $(seq 0 $((GPU_COUNT-1))); do\n'
    s += '  echo "--- GPU $i ---" >> "$LOG"\n'

    if not overrides:
        s += _emit(globals_, '  ')
    else:
        s += '  case "$i" in\n'
        for idx in sorted(overrides):
            merged = {**globals_, **{k: v for k, v in overrides[idx].items()
                                     if k != "gpu" and v is not None}}
            s += f'    {idx})\n'
            s += _emit(merged, '      ')
            s += '      ;;\n'
        s += '    *)\n'
        s += _emit(globals_, '      ')
        s += '      ;;\n'
        s += '  esac\n'

    s += 'done\n'
    # Snapshot what actually took. Operators reading oc.log can immediately
    # see whether `-lgc` stuck (clocks.gr.lock vs. clocks.gr).
    s += 'echo "--- post-apply state ---" >> "$LOG"\n'
    s += 'nvidia-smi --query-gpu=index,name,clocks.gr,clocks.max.gr,power.limit,fan.speed,persistence_mode --format=csv,noheader >> "$LOG" 2>&1\n'
    s += f'echo "===== OC {label} done at $(date) =====" >> "$LOG"\n'
    # Make the log world-readable so the dashboard can tail it without sudo.
    s += 'chmod 755 /var/log/mfarm 2>/dev/null || true\n'
    s += 'chmod 644 "$LOG" 2>/dev/null || true\n'
    return s


def _sudo_install_file(rig: Rig, content: str, path: str, mode: str) -> None:
    """Write `content` to `path` on `rig` as root, bypassing SFTP.

    paramiko's SFTP `open(path, "w")` fails with EACCES whenever a stale
    root-owned file exists at the (typically /tmp) staging path. We avoid
    /tmp entirely by base64-piping the content through `sudo tee` straight
    to the destination — robust against any tmp permissions, ownership,
    or chattr +i state.
    """
    pool = get_pool()
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    qpath = shlex.quote(path)
    cmd = (
        f"echo {b64} | base64 -d | sudo tee {qpath} > /dev/null && "
        f"sudo chmod {mode} {qpath}"
    )
    stdout, stderr, rc = pool.exec(rig, cmd, timeout=15)
    if rc != 0:
        raise RuntimeError(
            f"sudo install of {path} failed (rc={rc}): {stderr.strip() or 'no stderr'}"
        )


async def _push_oc_to_rig(rig: Rig, profile: OcProfile, label: str) -> None:
    """Upload apply-oc.sh + mfarm-oc.service to `rig` and run apply now.

    Caller is responsible for setting `rig.oc_profile_id` and saving.
    Raises on any underlying SSH/exec failure.
    """
    pool = get_pool()
    loop = asyncio.get_event_loop()
    oc_script = _build_oc_script(profile, label)
    svc = ('[Unit]\nDescription=MeowFarm GPU Overclock\nAfter=multi-user.target\n'
           'Wants=mfarm-agent.service\n\n[Service]\nType=oneshot\n'
           'ExecStart=/opt/mfarm/apply-oc.sh\nRemainAfterExit=yes\n\n'
           '[Install]\nWantedBy=multi-user.target\n')

    await loop.run_in_executor(
        _executor, lambda: pool.exec(rig,
            "sudo mkdir -p /opt/mfarm /var/log/mfarm", timeout=5)
    )
    await loop.run_in_executor(
        _executor, lambda: _sudo_install_file(rig, oc_script, "/opt/mfarm/apply-oc.sh", "0755")
    )
    await loop.run_in_executor(
        _executor, lambda: _sudo_install_file(rig, svc, "/etc/systemd/system/mfarm-oc.service", "0644")
    )
    await loop.run_in_executor(
        _executor, lambda: pool.exec(rig,
            "sudo systemctl daemon-reload && sudo systemctl enable mfarm-oc.service",
            timeout=10)
    )
    await loop.run_in_executor(
        _executor, lambda: pool.exec(rig, "sudo bash /opt/mfarm/apply-oc.sh", timeout=30)
    )


@router.post("/oc-profiles/{oc_name}/apply/{target}")
async def apply_oc_profile(oc_name: str, target: str):
    """Apply OC profile to rig(s) and deploy a persistent boot script."""
    from mfarm.targets import resolve_targets
    db = get_db()
    profile = OcProfile.get_by_name(db, oc_name)
    if not profile:
        raise HTTPException(404, f"OC profile '{oc_name}' not found")

    rigs = resolve_targets(db, target)
    results = {}
    for rig in rigs:
        try:
            rig.oc_profile_id = profile.id
            rig.save(db)
            await _push_oc_to_rig(rig, profile, oc_name)
            results[rig.name] = "applied (persists on reboot)"
        except Exception as e:
            results[rig.name] = f"error: {e}"

    return {"results": results}


# ── Inline OC: per-rig apply without a named profile ─────────────────
# The frontend's "Apply OC" card hits these endpoints directly with
# raw values. We persist them via a hidden `__inline_<rigname>` profile
# so the existing apply machinery + boot-time systemd unit work as-is.

class InlineOcBody(BaseModel):
    core_offset: int | None = None
    mem_offset: int | None = None
    core_lock: int | None = None
    mem_lock: int | None = None
    power_limit: int | None = None
    fan_speed: int | None = None
    # Optional per-GPU overrides. Each entry: {"gpu": <index>, "core_offset"?,
    # "mem_offset"?, "core_lock"?, "mem_lock"?, "power_limit"?, "fan_speed"?}.
    # Any field omitted on a per-GPU entry falls back to the global value above.
    per_gpu: list[dict] | None = None


def _inline_profile_name(rig_name: str) -> str:
    return f"__inline_{rig_name}"


@router.get("/rigs/{name}/oc")
def get_rig_oc(name: str):
    """Return the current OC values for the rig's bound profile (if any)."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"rig '{name}' not found")
    empty = {"core_offset": None, "mem_offset": None, "core_lock": None,
             "mem_lock": None, "power_limit": None, "fan_speed": None,
             "per_gpu": None, "profile_name": None, "is_inline": False}
    if not rig.oc_profile_id:
        return empty
    p = next((q for q in OcProfile.get_all(db) if q.id == rig.oc_profile_id), None)
    if not p:
        return empty
    is_inline = p.name == _inline_profile_name(name)
    return {
        "core_offset": p.core_offset, "mem_offset": p.mem_offset,
        "core_lock": p.core_lock, "mem_lock": p.mem_lock,
        "power_limit": p.power_limit, "fan_speed": p.fan_speed,
        "per_gpu": p.per_gpu,
        "profile_name": None if is_inline else p.name,
        "is_inline": is_inline,
    }


@router.post("/rigs/{name}/oc")
async def apply_rig_oc(name: str, body: InlineOcBody):
    """Apply inline OC values to a single rig.

    Persists the values into a hidden `__inline_<name>` profile, links the
    rig to it, then runs the same apply pipeline as profile-apply.
    """
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"rig '{name}' not found")

    pname = _inline_profile_name(name)
    p = OcProfile.get_by_name(db, pname)
    if p is None:
        p = OcProfile(name=pname)
    p.core_offset = body.core_offset
    p.mem_offset = body.mem_offset
    p.core_lock = body.core_lock
    p.mem_lock = body.mem_lock
    p.power_limit = body.power_limit
    p.fan_speed = body.fan_speed
    p.per_gpu_overrides = json.dumps(body.per_gpu) if body.per_gpu else None
    p.save(db)

    rig.oc_profile_id = p.id
    rig.save(db)

    try:
        await _push_oc_to_rig(rig, p, pname)
    except Exception as e:
        raise HTTPException(500, f"apply failed: {e}")

    # Tail oc.log so the UI can show what nvidia-smi/-settings actually said.
    # Useful for diagnosing silent `-lgc` rejects on consumer cards. Always
    # return a non-empty string so the operator can see *something*; if the
    # log is missing, fall back to ls of the dir + script so we know whether
    # the script even ran.
    cmd = (
        "echo '--- /var/log/mfarm/oc.log (tail 80) ---'; "
        "(sudo tail -n 80 /var/log/mfarm/oc.log 2>&1 "
        "  || tail -n 80 /var/log/mfarm/oc.log 2>&1 "
        "  || echo '[oc.log unreadable]'); "
        "echo; echo '--- ls /var/log/mfarm /opt/mfarm ---'; "
        "ls -la /var/log/mfarm/ /opt/mfarm/apply-oc.sh 2>&1 || true"
    )
    log_tail = ""
    try:
        pool = get_pool()
        loop = asyncio.get_event_loop()
        stdout, _, _ = await loop.run_in_executor(
            _executor, lambda: pool.exec(rig, cmd, timeout=5),
        )
        log_tail = stdout or "(empty stdout from tail; sudo may be unavailable)"
    except Exception as e:
        log_tail = f"(failed to read oc.log: {type(e).__name__}: {e})"
    return {"status": "applied", "log": log_tail}


@router.post("/rigs/{name}/oc/reset")
async def reset_rig_oc(name: str):
    """Clear OC on a rig: reset clocks/power, disable boot unit, unlink profile."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"rig '{name}' not found")

    pool = get_pool()
    loop = asyncio.get_event_loop()
    reset_sh = (
        'GPU_COUNT=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l); '
        'for i in $(seq 0 $((GPU_COUNT-1))); do '
        '  nvidia-smi -i $i -rgc > /dev/null 2>&1; '
        '  nvidia-smi -i $i -rmc > /dev/null 2>&1; '
        '  nvidia-smi -i $i -pl 0 > /dev/null 2>&1 || true; '
        'done; '
        'sudo systemctl disable --now mfarm-oc.service 2>/dev/null; '
        'sudo rm -f /etc/systemd/system/mfarm-oc.service /opt/mfarm/apply-oc.sh; '
        'sudo systemctl daemon-reload; '
        'echo "OC reset at $(date)" >> /var/log/mfarm/oc.log'
    )
    try:
        await loop.run_in_executor(
            _executor, lambda: pool.exec(rig, f"sudo bash -c {shlex.quote(reset_sh)}", timeout=30)
        )
    except Exception as e:
        raise HTTPException(500, f"reset failed: {e}")

    rig.oc_profile_id = None
    rig.save(db)
    return {"status": "reset"}


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


@router.post("/refresh")
async def force_refresh():
    """Wake the rig poller so the next /ws stats_update arrives immediately."""
    from mfarm.web.app import _poll_wake
    _poll_wake.set()
    return {"status": "ok"}


@router.get("/discovered")
def get_discovered():
    """List rigs that have phoned home but aren't claimed or user-dismissed.

    Filter logic (skip dismissed MAC, auto-dismiss IP/hostname match) lives
    in app._filtered_discovered() so the WS push uses the same rules.
    """
    from mfarm.web.app import _filtered_discovered
    return _filtered_discovered()


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

# In-memory cache of algorithm lists discovered from real miner binaries on
# rigs. Keyed by miner name; value is (sorted_algo_list, expiry_unix_ts).
# TTL is 1 hour — long enough to avoid hammering rigs, short enough that
# a binary update is reflected within an hour without a server restart.
_DISCOVERED_ALGOS: dict[str, tuple[list[str], float]] = {}
_DISCOVERY_TTL_SEC = 3600


async def _discover_algos_for_miner(miner) -> list[str] | None:
    """Run miner --list-algorithms on an online rig and parse output.

    Returns the discovered list, or None if discovery couldn't complete
    (no online rig with the binary, command failed, or no parser registered).
    """
    if not miner.algo_query_argv:
        return None  # discovery not implemented for this miner
    db = get_db()
    pool = get_pool()
    loop = asyncio.get_event_loop()
    bin_path = miner.default_install_path
    args_str = " ".join(shlex.quote(a) for a in miner.algo_query_argv)
    if miner.algo_query_use_pty:
        # SRBMiner falls into "Guided setup" without a TTY; wrap in `script`.
        cmd = f"script -qc {shlex.quote(f'{bin_path} {args_str}')} /dev/null < /dev/null"
    else:
        cmd = f"{shlex.quote(bin_path)} {args_str}"
    cmd_wrapped = f"bash -lc {shlex.quote(cmd)}"

    # Try each rig in inventory until one succeeds.
    for rig in Rig.get_all(db):
        try:
            stdout, stderr, rc = await loop.run_in_executor(
                _executor, lambda r=rig: pool.exec(r, cmd_wrapped, timeout=10)
            )
        except Exception as e:
            logging.debug("algo discovery failed on %s: %s", rig.name, e)
            continue
        if rc != 0 and not stdout:
            continue
        algos = parse_algo_output(miner.name, stdout)
        if algos:
            logging.info("Discovered %d algos for %s from rig %s",
                         len(algos), miner.name, rig.name)
            return algos
    return None


async def _algos_for(miner) -> list[str]:
    """Cached algos with hardcoded fallback.

    Live discovery happens in `algo_refresh_loop` (background, hourly) — we
    do NOT block the request on it. Otherwise a cold cache turns /api/miners
    into a request that synchronously SSHes every rig × every miner and the
    dropdown freezes for 5+ minutes after every console restart.
    """
    now = _time.time()
    cached = _DISCOVERED_ALGOS.get(miner.name)
    if cached and cached[1] > now:
        return cached[0]
    return miner.supported_algos


@router.get("/miners")
async def get_miners():
    out = []
    for m in list_miners():
        algos = await _algos_for(m)
        out.append({"name": m.name, "display_name": m.display_name,
                    "gpu_type": m.gpu_type, "algos": algos,
                    "supports_solo": m.supports_solo})
    return out


@router.post("/miners/refresh")
async def refresh_miner_algos():
    """Force re-discovery of algorithm lists for all miners.

    Unlike /api/miners (which is read-only and uses cache-or-fallback),
    this endpoint synchronously runs the live SSH discovery so the user
    can wait through it after pressing the dashboard's refresh button.
    """
    _DISCOVERED_ALGOS.clear()
    now = _time.time()
    out = []
    for m in list_miners():
        discovered = await _discover_algos_for_miner(m)
        if discovered:
            _DISCOVERED_ALGOS[m.name] = (discovered, now + _DISCOVERY_TTL_SEC)
        out.append({"name": m.name,
                    "discovered": len(discovered) if discovered else 0,
                    "from_binary": m.name in _DISCOVERED_ALGOS})
    return out


async def algo_refresh_loop():
    """Background task: discover algos for every miner periodically.

    Runs at server startup (via lifespan) so the dropdown is populated
    even before any user opens it. Repeats hourly so binary updates
    (via 'Update Miners') propagate without a manual refresh click.
    """
    # Brief startup delay to let the rig poll task warm up the SSH pool
    # and inventory load.
    await asyncio.sleep(15)
    while True:
        try:
            _DISCOVERED_ALGOS.clear()
            for m in list_miners():
                await _algos_for(m)
            logging.info("algo refresh complete: %d miners discovered live",
                         len(_DISCOVERED_ALGOS))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning("algo refresh failed: %s", e)
        # Sleep just under the cache TTL so /api/miners always serves
        # cache hits and the discovery work happens off the request path.
        await asyncio.sleep(_DISCOVERY_TTL_SEC - 60)


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


# ── Vast.ai install ─────────────────────────────────────────────────

class VastInstallRequest(BaseModel):
    token: str
    port_base: int = 41


@router.post("/rigs/{name}/install-vast")
async def install_vast(name: str, body: VastInstallRequest):
    """Push install-vast.sh to the rig and launch it under nohup.

    The script (mfarm/worker/install-vast.sh) is the canonical Vast host
    install — it does partition resize, service masking, sudoers env_keep,
    docker .deb pre-cache, then runs Vast's installer in a screen session
    on the rig and applies the post-install fixes (vast_metrics ExecStartPre
    chmod, send_mach_info push). See reference_vastai_install.md for the
    full list of gotchas it handles.

    The script itself runs the Vast installer detached (`screen -dmS
    vastinstall`), so the launch returns within a few seconds even though
    the install continues on the rig for ~10 min. Frontend should poll
    /api/rigs/{name}/vast-install-log to stream progress.
    """
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")

    token = body.token.strip()
    if not token or len(token) < 30:
        raise HTTPException(400, "token looks invalid (too short)")
    if not (10 <= body.port_base <= 99):
        raise HTTPException(400, "port_base must be 10–99 (gives 10000–99200)")

    from pathlib import Path
    script_path = Path(__file__).resolve().parent.parent / "worker" / "install-vast.sh"
    if not script_path.is_file():
        raise HTTPException(500, f"install-vast.sh missing at {script_path}")
    script = script_path.read_text(encoding="utf-8")

    pool = get_pool()
    loop = asyncio.get_event_loop()

    try:
        # Push the script
        await loop.run_in_executor(
            _executor,
            lambda: pool.upload_string(rig, script, "/tmp/install-vast.sh"),
        )
        # Launch the install fully detached. The wrapping subshell `(...)` plus
        # `setsid` ensures the spawned process has no parent shell or
        # controlling terminal — without this, paramiko's exec_command can
        # hang waiting for stdout EOF (the disowned background process keeps
        # parent fds, channel never closes, channel.recv times out and the
        # str() of the resulting socket.timeout is empty, surfacing to the
        # dashboard as the unhelpful "install-vast launch failed:" message).
        # The script's own monitor loop is the long-running part; we just
        # kick it off and return. stdout/stderr go to /tmp/vast-runner.log,
        # which the log endpoint tails.
        cmd = (
            f"chmod +x /tmp/install-vast.sh && "
            f"rm -f /tmp/vast-runner.log && "
            f"( setsid bash /tmp/install-vast.sh {shlex.quote(token)} {body.port_base} "
            f"  > /tmp/vast-runner.log 2>&1 < /dev/null & ) && "
            f"echo started"
        )
        out, _, rc = await loop.run_in_executor(
            _executor, lambda: pool.exec(rig, cmd, timeout=15)
        )
        if rc != 0 or "started" not in out:
            raise HTTPException(500, f"failed to launch install ({rc}): {out}")
        return {"status": "started", "port_base": body.port_base, "log_path": "/tmp/vast-runner.log"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"install-vast launch failed: {e}")


@router.get("/rigs/{name}/vast-install-log")
async def vast_install_log(name: str, lines: int = 80):
    """Tail the Vast install log on the rig and report whether it's still running.

    Reads /tmp/vast-runner.log (the script's own stdout) and concatenates
    the tail of /var/log/vast-install.log (the screen-session installer
    output) so the frontend gets both layers in one fetch.
    """
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404)
    pool = get_pool()
    loop = asyncio.get_event_loop()
    cmd = (
        f"echo '--- /tmp/vast-runner.log ---'; "
        f"tail -n {lines} /tmp/vast-runner.log 2>/dev/null; "
        f"echo '--- /var/log/vast-install.log ---'; "
        f"tail -n {lines} /var/log/vast-install.log 2>/dev/null; "
        f"echo '---'; "
        f"pgrep -af 'install-vast.sh|python3 /tmp/install' | grep -v 'pgrep\\|grep' | wc -l"
    )
    try:
        out, _, _ = await loop.run_in_executor(
            _executor, lambda: pool.exec(rig, cmd, timeout=8)
        )
        # Last line is the running-process count
        parts = out.rstrip().rsplit("\n---\n", 1)
        if len(parts) == 2:
            log_text, tail = parts
            try:
                running = int(tail.strip().splitlines()[-1]) > 0
            except Exception:
                running = False
        else:
            log_text, running = out, False
        return {"log": log_text, "running": running}
    except Exception as e:
        return {"log": f"error reading log: {e}", "running": False}


# ── Bulk actions ────────────────────────────────────────────────────

# Map of bulk action -> per-rig coroutine factory. Each factory returns a
# coroutine that runs the action against a single Rig and returns a string
# (status / error). Adding a new bulk action = adding an entry here.
def _bulk_actions(rig: Rig, pool, loop, command: str | None) -> dict:
    """Return the registry of bulk actions valid for `rig`. Each entry's
    `run` is an async callable taking no args."""
    async def reboot():
        try:
            await loop.run_in_executor(_executor, lambda: pool.exec(rig, "sudo reboot", timeout=5))
        except Exception:
            pass  # SSH disconnects on reboot, treat as success
        return "rebooting"

    async def shutdown():
        try:
            await loop.run_in_executor(_executor, lambda: pool.exec(rig, "sudo poweroff", timeout=5))
        except Exception:
            pass
        return "shutting down"

    async def restart_miner():
        await loop.run_in_executor(
            _executor,
            lambda: pool.upload_string(rig, "restart_miner", "/var/run/mfarm/command"),
        )
        return "restart sent"

    async def stop_miner():
        await loop.run_in_executor(
            _executor,
            lambda: pool.upload_string(rig, "stop_miner", "/var/run/mfarm/command"),
        )
        return "stop sent"

    async def start_miner():
        await loop.run_in_executor(
            _executor,
            lambda: pool.upload_string(rig, "start_miner", "/var/run/mfarm/command"),
        )
        return "start sent"

    async def update_miners():
        # Same pipeline as the single-rig endpoint: push fresh downloader,
        # repair gzipped blobs, restart agent, then run the downloader.
        ok, msg = await loop.run_in_executor(
            _executor, lambda: _push_downloader_and_repair(rig),
        )
        if not ok:
            return f"prep failed: {msg}"
        try:
            out, _, rc = await loop.run_in_executor(
                _executor,
                lambda: pool.exec(rig, "sudo bash /opt/mfarm/miner-downloader.sh all", timeout=300),
            )
            return f"updated (rc={rc}); {msg}"
        except Exception as e:
            return f"download failed: {e}; {msg}"

    async def exec_cmd():
        if not command:
            return "error: no command provided"
        # Same login-shell wrap as /api/rigs/{name}/exec — see that endpoint
        # for why (lets profile.d-defined functions like `miner` resolve).
        wrapped = f"bash -lc {shlex.quote(command)}"
        try:
            stdout, stderr, rc = await loop.run_in_executor(
                _executor, lambda: pool.exec(rig, wrapped, timeout=60),
            )
            # Trim long output so the UI summary stays readable. Full output
            # is still returned in the per-rig result for debugging.
            return {
                "rc": rc,
                "stdout": stdout[-2000:] if len(stdout) > 2000 else stdout,
                "stderr": stderr[-500:] if len(stderr) > 500 else stderr,
            }
        except Exception as e:
            return f"error: {e}"

    return {
        "reboot":         {"run": reboot,         "needs_confirm": True},
        "shutdown":       {"run": shutdown,       "needs_confirm": True},
        "restart-miner":  {"run": restart_miner,  "needs_confirm": False},
        "stop-miner":     {"run": stop_miner,     "needs_confirm": False},
        "start-miner":    {"run": start_miner,    "needs_confirm": False},
        "update-miners":  {"run": update_miners,  "needs_confirm": False},
        "exec":           {"run": exec_cmd,       "needs_confirm": True},
    }


@router.post("/bulk/{action}/{target}")
async def bulk_action(action: str, target: str, body: dict | None = None):
    """Run an action against a target spec (single rig name, comma-separated
    list, 'group:NAME', or 'all'). Returns per-rig results.

    Supported actions: reboot, shutdown, restart-miner, stop-miner,
    start-miner, update-miners, exec.

    For 'exec', POST body must be {"command": "shell command here"}.
    """
    from mfarm.targets import resolve_targets
    db = get_db()
    try:
        rigs = resolve_targets(db, target)
    except Exception as e:
        # ClickException carries the user-facing message in .message; fall
        # back to str() for any other exception type.
        raise HTTPException(400, getattr(e, "message", str(e)))

    pool = get_pool()
    loop = asyncio.get_event_loop()
    command = (body or {}).get("command")

    # Run the action against every rig in parallel — bulk reboot of 13 rigs
    # serially would take 13 × ssh-connect-timeout if any rig is unreachable.
    async def _one(rig: Rig) -> tuple[str, object]:
        actions = _bulk_actions(rig, pool, loop, command)
        if action not in actions:
            return rig.name, f"error: unknown action '{action}'"
        try:
            return rig.name, await actions[action]["run"]()
        except Exception as e:
            return rig.name, f"error: {e}"

    pairs = await asyncio.gather(*(_one(r) for r in rigs))
    return {"action": action, "results": dict(pairs)}


@router.get("/version")
def get_version():
    from mfarm import __version__
    return {"version": __version__}


# ── Agent auto-update ────────────────────────────────────────────────
# Rigs running MeowOS poll these endpoints (see meowos-updater.py / .timer)
# every ~5 min. /version is a cheap version check; /bundle returns a tarball
# of the worker files keyed to install paths on the rig. Trust model: plain
# HTTP on a trusted LAN, matching the existing phonehome posture.

import io as _io
import tarfile as _tarfile
from pathlib import Path as _Path

from fastapi.responses import StreamingResponse

_REPO_ROOT = _Path(__file__).resolve().parents[2]
_WORKER_DIR = _REPO_ROOT / "mfarm" / "worker"
_VERSION_FILE = _REPO_ROOT / "VERSION"

# arcname (rig path relative to /) → source path on console
_AGENT_BUNDLE_FILES = {
    "opt/mfarm/mfarm-agent.py":            _WORKER_DIR / "mfarm-agent.py",
    "opt/mfarm/miner-wrapper.sh":          _WORKER_DIR / "miner-wrapper.sh",
    "opt/mfarm/miner-downloader.sh":       _WORKER_DIR / "miner-downloader.sh",
    "opt/mfarm/meowos-phonehome.py":       _WORKER_DIR / "meowos-phonehome.py",
    "opt/mfarm/meowos-webui.py":           _WORKER_DIR / "meowos-webui.py",
    "opt/mfarm/meowos-webui.html":         _WORKER_DIR / "meowos-webui.html",
    "opt/mfarm/meowos-updater.py":         _WORKER_DIR / "meowos-updater.py",
    "opt/mfarm/meowos-dpkg-health.sh":     _WORKER_DIR / "meowos-dpkg-health.sh",
    "opt/mfarm/fan_controller_cli":        _WORKER_DIR / "fan_controller_cli",
    "etc/systemd/system/mfarm-agent.service":         _WORKER_DIR / "mfarm-agent.service",
    "etc/systemd/system/meowos-dpkg-health.service":  _WORKER_DIR / "meowos-dpkg-health.service",
    "etc/systemd/system/meowos-phonehome.service":    _WORKER_DIR / "meowos-phonehome.service",
    "etc/systemd/system/meowos-webui.service":        _WORKER_DIR / "meowos-webui.service",
    "etc/systemd/system/meowos-updater.service":      _WORKER_DIR / "meowos-updater.service",
    "etc/systemd/system/meowos-updater.timer":        _WORKER_DIR / "meowos-updater.timer",
    "etc/systemd/system/xmrig-1gb-hugepages.service": _WORKER_DIR / "xmrig-1gb-hugepages.service",
    "usr/local/bin/miner":                 _WORKER_DIR / "miner-attach.sh",
    # VERSION must come last — the rig uses its presence as the "extract OK"
    # marker before installing anything.
    "opt/mfarm/VERSION":                   _VERSION_FILE,
}


def _agent_version() -> str:
    try:
        from mfarm import __version__
        if __version__ and __version__ != "unknown":
            return __version__
    except Exception:
        pass
    try:
        return _VERSION_FILE.read_text().strip()
    except OSError:
        return "0.0.0"


@router.get("/agent/version")
def get_agent_version():
    return {"version": _agent_version()}


@router.get("/agent/bundle")
def get_agent_bundle():
    """Tarball (gzip) of agent files for rig auto-update.

    Tar entries use rig install paths relative to '/' (e.g.
    'opt/mfarm/mfarm-agent.py'). The rig updater rejects any path outside
    a fixed allow-list.
    """
    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arc, src in _AGENT_BUNDLE_FILES.items():
            if src.exists():
                tar.add(str(src), arcname=arc)
    return StreamingResponse(
        _io.BytesIO(buf.getvalue()),
        media_type="application/gzip",
        headers={"X-Agent-Version": _agent_version()},
    )


# ── Router integration ──────────────────────────────────────────────
# Pluggable backends create port-forward rules on the operator's router so
# Vast hosts get verified without manual UniFi/router clicks per rig. See
# mfarm/router/ for the abstraction; UnifiBackend is the only real
# implementation today (manual is a no-op fallback).

class RouterConfigUpdate(BaseModel):
    backend: str
    config: dict


def _read_rig_port_range(rig: Rig) -> tuple[int, int] | None:
    """Read /var/lib/vastai_kaalia/host_port_range from the rig over SSH.
    Returns None when the rig isn't a Vast host (no file) or unreachable.

    File format varies: some Vast versions write '40000 40200', others
    write '40000-40200'. Tolerate both."""
    try:
        pool = get_pool()
        out, _, rc = pool.exec(rig, "cat /var/lib/vastai_kaalia/host_port_range 2>/dev/null", timeout=5)
        if rc != 0 or not out.strip():
            return None
        parts = re.split(r'[-\s]+', out.strip())
        if len(parts) != 2:
            return None
        return (int(parts[0]), int(parts[1]))
    except Exception:
        return None


def _redact_config(config: dict) -> dict:
    """Strip secret-looking fields before returning to the client."""
    safe = dict(config)
    for k in ("password", "token", "api_key", "secret"):
        if k in safe and safe[k]:
            safe[k] = "********"
    return safe


@router.get("/router/config")
def get_router_config():
    """Return the active backend name + config with secrets redacted."""
    from mfarm.router.store import load_config
    db = get_db()
    backend, config = load_config(db)
    return {
        "backend": backend,
        "config": _redact_config(config),
        "available_backends": list(__import__("mfarm.router", fromlist=["BACKENDS"]).BACKENDS.keys()),
    }


@router.put("/router/config")
def put_router_config(body: RouterConfigUpdate):
    """Save backend name + config. Validates before persisting; returns 400
    if config is missing required fields for the chosen backend."""
    from mfarm.router import BACKENDS
    from mfarm.router.base import ConfigError
    from mfarm.router.store import load_config, save_config
    if body.backend not in BACKENDS:
        raise HTTPException(400, f"unknown backend: {body.backend}. Valid: {list(BACKENDS)}")
    db = get_db()
    # If caller passed back redacted "********" for a secret, restore the
    # stored value — typical PUT-after-GET roundtrip from a settings UI.
    existing_backend, existing_config = load_config(db)
    merged = dict(body.config)
    if existing_backend == body.backend:
        for k in ("password", "token", "api_key", "secret"):
            if merged.get(k) == "********" and existing_config.get(k):
                merged[k] = existing_config[k]
    try:
        save_config(db, body.backend, merged)
    except ConfigError as e:
        raise HTTPException(400, str(e))
    return {"status": "saved", "backend": body.backend}


@router.post("/router/test")
def test_router_connection():
    """Probe the configured backend (e.g. UniFi login + list rules).
    Returns ApplyResult-style {ok, messages}."""
    from mfarm.router.store import current_backend
    db = get_db()
    backend = current_backend(db)
    res = backend.test_connection()
    return {"ok": res.ok, "messages": res.messages}


@router.post("/router/sync")
async def sync_router_rules():
    """Apply port-forward rules for every claimed rig that has a Vast port
    range. Idempotent — safe to re-run. Returns per-rig ApplyResult."""
    from mfarm.router.base import ForwardRule
    from mfarm.router.store import current_backend
    db = get_db()
    backend = current_backend(db)
    rigs = Rig.get_all(db)
    loop = asyncio.get_event_loop()
    results: dict[str, dict] = {}
    for rig in rigs:
        rng = await loop.run_in_executor(_executor, lambda r=rig: _read_rig_port_range(r))
        if rng is None:
            results[rig.name] = {"ok": True, "messages": ["skipped: no Vast port range (not a Vast host or unreachable)"]}
            continue
        rule = ForwardRule(
            rig_name=rig.name,
            internal_ip=rig.host,
            port_lo=rng[0],
            port_hi=rng[1],
        )
        res = await loop.run_in_executor(_executor, lambda b=backend, r=rule: b.apply_rule(r))
        results[rig.name] = {"ok": res.ok, "messages": res.messages}
    return {"results": results}


@router.post("/router/sync/{rig_name}")
async def sync_router_rule(rig_name: str):
    """Apply (or remove if no port range) the rule for one rig. Used as a
    hook from rig add/update flows."""
    from mfarm.router.base import ForwardRule
    from mfarm.router.store import current_backend
    db = get_db()
    rig = Rig.get_by_name(db, rig_name)
    if not rig:
        raise HTTPException(404, f"rig {rig_name} not found")
    backend = current_backend(db)
    loop = asyncio.get_event_loop()
    rng = await loop.run_in_executor(_executor, lambda: _read_rig_port_range(rig))
    if rng is None:
        # No Vast on this rig — make sure no stale rule is left
        res = await loop.run_in_executor(_executor, lambda: backend.remove_rule(rig_name))
        return {"ok": res.ok, "messages": res.messages, "action": "removed (no vast)"}
    rule = ForwardRule(
        rig_name=rig.name,
        internal_ip=rig.host,
        port_lo=rng[0],
        port_hi=rng[1],
    )
    res = await loop.run_in_executor(_executor, lambda: backend.apply_rule(rule))
    return {"ok": res.ok, "messages": res.messages, "action": "applied"}


@router.delete("/router/rules/{rig_name}")
async def remove_router_rule(rig_name: str):
    """Manually remove a rig's rule. Called automatically when a rig is
    deleted (see rig DELETE handler)."""
    from mfarm.router.store import current_backend
    db = get_db()
    backend = current_backend(db)
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(_executor, lambda: backend.remove_rule(rig_name))
    return {"ok": res.ok, "messages": res.messages}
