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
from mfarm.web.api import router as api_router

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Global stats cache
_stats_cache: dict[str, dict | None] = {}
_stats_errors: dict[str, str | None] = {}
_poll_task: asyncio.Task | None = None
_ws_clients: set[WebSocket] = set()


async def poll_all_rigs():
    """Background task that polls rig stats and pushes to WebSocket clients."""
    global _stats_cache, _stats_errors
    pool = get_pool()
    executor = ThreadPoolExecutor(max_workers=10)

    while True:
        try:
            db = get_db()
            rigs = Rig.get_all(db)

            if rigs:
                loop = asyncio.get_event_loop()
                futures = []
                for rig in rigs:
                    futures.append((rig, loop.run_in_executor(executor, _poll_one, pool, rig)))

                for rig, future in futures:
                    try:
                        stats = await asyncio.wait_for(future, timeout=10)
                        _stats_cache[rig.name] = stats
                        _stats_errors[rig.name] = None
                    except Exception as e:
                        _stats_cache[rig.name] = None
                        _stats_errors[rig.name] = str(e)

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

        await asyncio.sleep(2)


def _poll_one(pool, rig: Rig) -> dict | None:
    try:
        stdout, _, rc = pool.exec(rig, "cat /var/run/mfarm/stats.json", timeout=5)
        if rc == 0 and stdout.strip():
            return json.loads(stdout)
    except Exception:
        pass
    return None


def _build_rigs_payload() -> list[dict]:
    db = get_db()
    rigs = Rig.get_all(db)
    result = []
    for rig in rigs:
        stats = _stats_cache.get(rig.name)
        error = _stats_errors.get(rig.name)
        entry = {
            "name": rig.name,
            "host": rig.host,
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
    """Listen for phone-home UDP broadcasts from MeowOS rigs."""
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 8889))
    sock.setblocking(False)

    while True:
        try:
            data = await loop.sock_recv(sock, 4096)
            msg = json.loads(data.decode())
            if msg.get("type") == "phonehome":
                _handle_phonehome(msg)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task
    _poll_task = asyncio.create_task(poll_all_rigs())
    _udp_task = asyncio.create_task(udp_listener())
    yield
    if _poll_task:
        _poll_task.cancel()
    _udp_task.cancel()


app = FastAPI(title="MeowFarm Dashboard", lifespan=lifespan)
app.include_router(api_router, prefix="/api")
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
