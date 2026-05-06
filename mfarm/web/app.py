"""MFarm Web Dashboard - FastAPI application."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from mfarm.db.connection import get_db
from mfarm.db.models import Rig, FlightSheet, OcProfile, Group
from mfarm.ssh.pool import get_pool
from mfarm.web import agent_state
from mfarm.web.api import router as api_router, PUSH_STATS_TTL_SECS
from mfarm.web.agent_api import router as agent_router

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Global stats cache
_stats_cache: dict[str, dict | None] = {}
_stats_errors: dict[str, str | None] = {}
_poll_task: asyncio.Task | None = None
_ws_clients: set[WebSocket] = set()
# Set by POST /api/refresh (or any other early-wake source) to skip the
# 2-second sleep and re-poll immediately.
_poll_wake: asyncio.Event = asyncio.Event()

# Per-rig timestamp of the last rig_snapshots row we wrote. Used to throttle
# inserts to ~once per minute even though the poll loop runs every 2s.
_last_snapshot: dict[str, float] = {}
SNAPSHOT_INTERVAL_SECS = 60


def _record_snapshots(now: float):
    """Insert one row per online rig into rig_snapshots, throttled to ~60s.

    Reads from _stats_cache (populated by the poll loop). Skips rigs that are
    offline (stats is None) — empty rows just clutter the chart.
    """
    db = get_db()
    rows_to_insert = []
    for rig_name, stats in list(_stats_cache.items()):
        if not stats:
            continue
        if now - _last_snapshot.get(rig_name, 0) < SNAPSHOT_INTERVAL_SECS:
            continue
        m = stats.get("miner") or {}
        cm = stats.get("cpu_miner") or {}
        # Pick the active miner the same way the dashboard cards do.
        active = m if m.get("running") else (cm if cm.get("running") else m)
        hr = active.get("hashrate") or 0
        algo = active.get("algo") or active.get("name") or "unknown"
        gpu_pwr = sum((g.get("power_draw") or 0) for g in (stats.get("gpus") or []))
        cpu_pwr = (stats.get("cpu") or {}).get("power_draw") or 0
        pwr = gpu_pwr + cpu_pwr
        acc = (m.get("accepted") or 0) + (cm.get("accepted") or 0)
        rej = (m.get("rejected") or 0) + (cm.get("rejected") or 0)
        row = db.execute("SELECT id FROM rigs WHERE name = ?", (rig_name,)).fetchone()
        if not row:
            continue
        rows_to_insert.append((row[0], hr, pwr, algo, acc, rej))
        _last_snapshot[rig_name] = now
    if rows_to_insert:
        db.executemany(
            "INSERT INTO rig_snapshots (rig_id, hashrate, power_draw, algo, accepted, rejected) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows_to_insert,
        )
        db.commit()


def _take_pushed_stats(rig: Rig) -> dict | None:
    """If the rig's agent has pushed within PUSH_STATS_TTL_SECS, return that
    cached payload. Otherwise None — caller should fall back to SSH.

    Vast.ai hosts mask mfarm-agent (see _poll_one) and therefore cannot push,
    so they will always fall through to the SSH branch — that's where the
    `_vastai_active` flag gets attached, which the dashboard renders as a
    distinct status.
    """
    if agent_state.stats_age(rig.id) >= PUSH_STATS_TTL_SECS:
        return None
    return agent_state.get(rig.id).last_stats


async def poll_all_rigs():
    """Background task that polls rig stats and pushes to WebSocket clients.

    Two-phase: pushed rigs are taken from the in-memory cache (zero SSH
    cost), the remainder are SSH-probed in parallel. As more of the fleet
    moves onto the push transport the SSH fan-out shrinks correspondingly.
    """
    global _stats_cache, _stats_errors
    pool = get_pool()
    # One worker per rig so a slow SSH probe never blocks others. 50 covers
    # the current fleet (~41 rigs) with headroom; threads are cheap.
    executor = ThreadPoolExecutor(max_workers=50)

    while True:
        try:
            db = get_db()
            rigs = Rig.get_all(db)

            need_ssh: list[Rig] = []
            for rig in rigs:
                pushed = _take_pushed_stats(rig)
                if pushed is not None:
                    _stats_cache[rig.name] = pushed
                    _stats_errors[rig.name] = None
                else:
                    need_ssh.append(rig)

            if need_ssh:
                loop = asyncio.get_event_loop()
                futures = []
                for rig in need_ssh:
                    futures.append((rig, loop.run_in_executor(executor, _poll_one, pool, rig)))

                for rig, future in futures:
                    try:
                        # Outer cap slightly above _poll_one's SSH timeout so a
                        # single hung rig can't stall the whole cycle past 2s.
                        stats = await asyncio.wait_for(future, timeout=4)
                        _stats_cache[rig.name] = stats
                        _stats_errors[rig.name] = None
                    except Exception as e:
                        _stats_cache[rig.name] = None
                        _stats_errors[rig.name] = str(e)

            # Persist a snapshot row per rig (throttled inside) so the
            # per-rig hashrate/power history charts have data.
            try:
                _record_snapshots(time.time())
            except Exception as e:
                log.error("Snapshot record error: %s", e)

            # Push to all WebSocket clients
            payload = json.dumps({
                "type": "stats_update",
                "timestamp": time.time(),
                "rigs": _build_rigs_payload(),
            })

            dead = []
            for ws in list(_ws_clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _ws_clients.discard(ws)

        except Exception as e:
            log.error("Poll error: %s", e)

        # Sleep up to 2s, but wake immediately if anyone (e.g. the Refresh
        # button) sets _poll_wake.
        try:
            await asyncio.wait_for(_poll_wake.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        _poll_wake.clear()


def _poll_one(pool, rig: Rig) -> dict | None:
    """Probe a rig over SSH. Returns:
      - None when SSH itself fails (rig truly unreachable → OFFLINE)
      - dict otherwise. May contain mfarm stats keys (miner, gpus, cpu, …)
        when mfarm-agent is running, plus a "_vastai_active" flag when the
        rig is hosting Vast.ai (mfarm-agent is masked on Vast hosts so
        stats.json won't exist there — but the rig is still online).
    """
    try:
        # Combined probe: connectivity marker + vastai status + mfarm stats.
        # The marker line lets us distinguish "ssh worked, no miner" from
        # "ssh failed entirely" — without it both look like empty stdout.
        #
        # The vastai line MUST be exactly one line. The naive `is-active ||
        # echo missing` pattern produces TWO lines when is-active outputs
        # "inactive\n" with rc=3 (which is the common case on non-Vast rigs)
        # — both stdouts get concatenated. Using $(cmd1 || cmd2) doesn't fix
        # this either because the captured value is still both outputs.
        # Capture-then-default-via-${VAR:-fallback} gives us exactly one
        # line: VAR holds whatever is-active wrote (or empty string if it
        # failed entirely with rc=4 unit-not-found), then ${VAR:-missing}
        # outputs the value or "missing" if empty.
        #
        # Without this, lines[2] starts with "missing\n{...}" instead of
        # "{...}", the startswith("{") test fails, stats end up an empty
        # dict, and every mining rig appears IDLE in the dashboard.
        cmd = (
            "echo OK_REACHABLE; "
            "VAST=$(systemctl is-active vastai 2>/dev/null); echo \"${VAST:-missing}\"; "
            "cat /var/run/mfarm/stats.json 2>/dev/null"
        )
        stdout, _, rc = pool.exec(rig, cmd, timeout=3)
        if rc != 0 or not stdout.startswith("OK_REACHABLE"):
            return None
        lines = stdout.split("\n", 2)
        result: dict = {}
        # Line 1: vastai status — "active" / "inactive" / "missing" (no unit)
        if len(lines) > 1:
            vastai_state = lines[1].strip()
            if vastai_state == "active":
                result["_vastai_active"] = True
        # Line 2+: mfarm stats.json contents (may be empty on Vast/idle rigs)
        if len(lines) > 2 and lines[2].strip().startswith("{"):
            try:
                result.update(json.loads(lines[2]))
            except Exception:
                pass
        return result
    except Exception:
        pass
    return None


def _build_rigs_payload() -> list[dict]:
    from mfarm.web.api import _ip_to_mac_table
    db = get_db()
    rigs = Rig.get_all(db)
    mac_table = _ip_to_mac_table()
    result = []
    for rig in rigs:
        stats = _stats_cache.get(rig.name)
        error = _stats_errors.get(rig.name)
        entry = {
            "name": rig.name,
            "host": rig.host,
            "mac": rig.mac or mac_table.get(rig.host),
            "group": rig.group_name,
            "flight_sheet": rig.flight_sheet_name,
            "oc_profile": rig.oc_profile_name,
            "agent_version": rig.agent_version,
            "gpu_list": rig.gpu_names,
            "cpu_model": rig.cpu_model,
            "os_info": rig.os_info,
            "online": stats is not None and error is None,
            "error": error,
            "stats": stats,
        }
        result.append(entry)
    return result


_discovered_rigs: dict[str, dict] = {}  # MAC -> {ip, hostname, last_seen}

# MACs that have been claimed (rig already in DB or user dismissed). Persists
# across server restarts so a once-added rig never reappears in the discovery
# popup even when its IP changes via DHCP. Keyed by MAC because that's the
# only stable identifier — IP rotates, hostname can be edited.
_dismissed_macs_path = Path("/var/lib/mfarm/dismissed_macs.json")
_dismissed_macs: set[str] = set()


def _load_dismissed_macs() -> set[str]:
    try:
        if _dismissed_macs_path.exists():
            return set(json.loads(_dismissed_macs_path.read_text()))
    except Exception as e:
        log.warning("Failed to load dismissed MACs: %s", e)
    return set()


def _save_dismissed_macs(macs: set[str]) -> None:
    try:
        _dismissed_macs_path.parent.mkdir(parents=True, exist_ok=True)
        _dismissed_macs_path.write_text(json.dumps(sorted(macs)))
    except Exception as e:
        log.warning("Failed to save dismissed MACs: %s", e)


_dismissed_macs = _load_dismissed_macs()


async def udp_listener():
    """Listen for phone-home UDP broadcasts from MeowOS rigs.

    On each phonehome, reply with our HTTP port so the rig can compute the
    console URL from the reply's source IP. This is what makes auto-update
    work cross-subnet: if the rig and console are on different /24s but the
    same L2 broadcast domain, the rig's gateway isn't us and HTTP discovery
    can't find us — but the broadcast still reaches us, and the reply gets
    routed back with our IP as source.
    """
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 8889))
    sock.setblocking(False)

    reply = json.dumps({"type": "phonehome-reply", "port": 8888}).encode()

    while True:
        try:
            data, addr = await loop.sock_recvfrom(sock, 4096)
            msg = json.loads(data.decode())
            if msg.get("type") == "phonehome":
                _handle_phonehome(msg)
                try:
                    await loop.sock_sendto(sock, reply, addr)
                except OSError:
                    pass
        except Exception:
            await asyncio.sleep(1)


def _filtered_discovered() -> list[dict]:
    """Return discovered rigs excluding dismissed-MAC and already-claimed rigs.

    Mirrors the filter in /api/discovered: skip dismissed MACs, auto-dismiss
    (and persist) any MAC whose IP/hostname matches an existing rig. Used by
    both /api/discovered and the WS push so a phone-home from a claimed rig
    can never re-render in the discovery popup.
    """
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


def _handle_phonehome(msg: dict):
    """Process a phone-home message from a rig."""
    hostname = msg.get("hostname", "unknown")
    for iface in msg.get("interfaces", []):
        mac = iface.get("mac", "").lower()
        ip = iface.get("ip", "")
        if mac and ip:
            _discovered_rigs[mac] = {
                "ip": ip,
                "hostname": hostname,
                "mac": mac,
                "interface": iface.get("name", ""),
                "last_seen": time.time(),
            }
            log.info("Phone-home: %s (%s) at %s", hostname, mac, ip)

    # Self-heal IP drift: if a phonehome's MAC matches a claimed rig that has
    # this MAC stored, update Rig.host when it differs (DHCP lease shifted).
    # Also backfill: when a rig.host matches but rig.mac is null, populate it
    # so future drift can be detected.
    _reconcile_rig_host_from_phonehome(msg)

    # Push discovery update to WebSocket clients (filtered)
    payload = json.dumps({
        "type": "discovery",
        "rigs": _filtered_discovered(),
    })
    for ws in list(_ws_clients):
        try:
            asyncio.create_task(ws.send_text(payload))
        except Exception:
            pass


def _reconcile_rig_host_from_phonehome(msg: dict) -> None:
    """Match phonehome interfaces against claimed rigs by MAC and self-heal
    IP drift. See _handle_phonehome for context.

    Two cases per interface:
      1. Drift: a Rig.mac matches the interface MAC and Rig.host != interface
         IP. Update Rig.host, drop the SSH pool's cached connection (which
         points at the stale host).
      2. Backfill: a Rig.host matches the interface IP but Rig.mac is null
         (rig was claimed before this feature existed, or before this rig's
         first phonehome). Populate Rig.mac so case 1 can fire on next drift.
    """
    try:
        db = get_db()
        all_rigs = Rig.get_all(db)
        host_changed_names: list[str] = []
        for iface in msg.get("interfaces", []):
            mac = (iface.get("mac") or "").lower()
            ip = iface.get("ip") or ""
            if not (mac and ip):
                continue
            # Case 1: drift
            rig_by_mac = next(
                (r for r in all_rigs if (r.mac or "").lower() == mac), None
            )
            if rig_by_mac is not None:
                if rig_by_mac.host != ip:
                    log.info(
                        "rig %s: host drifted %s -> %s (mac %s) — auto-updating",
                        rig_by_mac.name, rig_by_mac.host, ip, mac,
                    )
                    rig_by_mac.host = ip
                    rig_by_mac.save(db)
                    host_changed_names.append(rig_by_mac.name)
                continue
            # Case 2: backfill
            rig_by_host = next(
                (r for r in all_rigs if r.host == ip and not r.mac), None
            )
            if rig_by_host is not None:
                log.info(
                    "rig %s: backfilling mac=%s", rig_by_host.name, mac,
                )
                rig_by_host.mac = mac
                rig_by_host.save(db)

        if host_changed_names:
            from mfarm.ssh.pool import get_pool
            pool = get_pool()
            with pool._lock:
                for name in host_changed_names:
                    pool._clients.pop(name, None)
            # Re-apply router rules so the port forward points at the new IP.
            # Best-effort; backend.apply_rule is idempotent.
            try:
                from mfarm.web.api import _hook_router_apply
                for name in host_changed_names:
                    _hook_router_apply(name)
            except Exception as e:
                log.warning("router reapply on host change failed: %s", e)
    except Exception as e:
        log.warning("rig-host reconcile failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task
    _poll_task = asyncio.create_task(poll_all_rigs())
    _udp_task = asyncio.create_task(udp_listener())
    # Periodic discovery of miner algorithm lists from the actual binaries
    # on the rigs. Refreshes the cache every (TTL - 60s) so /api/miners
    # always serves cache hits.
    from mfarm.web.api import algo_refresh_loop
    _algo_task = asyncio.create_task(algo_refresh_loop())
    yield
    if _poll_task:
        _poll_task.cancel()
    _udp_task.cancel()
    _algo_task.cancel()


app = FastAPI(title="MeowFarm Dashboard", lifespan=lifespan)
app.include_router(api_router, prefix="/api")
app.include_router(agent_router, prefix="/api/agent")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)

    # Send initial state
    try:
        await ws.send_text(json.dumps({
            "type": "stats_update",
            "timestamp": time.time(),
            "rigs": _build_rigs_payload(),
        }))
    except Exception:
        pass

    try:
        while True:
            data = await ws.receive_text()
            # Handle commands from frontend
            try:
                msg = json.loads(data)
                if msg.get("type") == "exec":
                    await _handle_exec(ws, msg)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        _ws_clients.discard(ws)


async def _handle_exec(ws: WebSocket, msg: dict):
    """Handle remote command execution from the frontend."""
    rig_name = msg.get("rig")
    command = msg.get("command")
    if not rig_name or not command:
        return

    db = get_db()
    rig = Rig.get_by_name(db, rig_name)
    if not rig:
        return

    pool = get_pool()
    loop = asyncio.get_event_loop()

    try:
        stdout, stderr, rc = await loop.run_in_executor(
            None, lambda: pool.exec(rig, command, timeout=30)
        )
        await ws.send_text(json.dumps({
            "type": "exec_result",
            "rig": rig_name,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": rc,
        }))
    except Exception as e:
        await ws.send_text(json.dumps({
            "type": "exec_result",
            "rig": rig_name,
            "error": str(e),
        }))


def run_server(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
