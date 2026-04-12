"""REST API routes for MFarm web dashboard."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

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
    return _rig_to_dict(Rig.get_by_name(db, data.name))


@router.delete("/rigs/{name}")
def delete_rig(name: str):
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if not rig:
        raise HTTPException(404, f"Rig '{name}' not found")
    rig.delete(db)
    return {"status": "deleted"}


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


def _rig_to_dict(r: Rig) -> dict:
    return {
        "name": r.name, "host": r.host, "ssh_port": r.ssh_port,
        "ssh_user": r.ssh_user, "group": r.group_name,
        "flight_sheet": r.flight_sheet_name, "oc_profile": r.oc_profile_name,
        "agent_version": r.agent_version, "gpu_list": r.gpu_names,
        "cpu_model": r.cpu_model, "os_info": r.os_info, "notes": r.notes,
    }


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

            await loop.run_in_executor(
                _executor,
                lambda r=rig, c=json.dumps(config, indent=2): pool.upload_string(r, c, "/etc/mfarm/config.json")
            )
            await loop.run_in_executor(
                _executor,
                lambda r=rig: pool.upload_string(r, "apply_config", "/var/run/mfarm/command")
            )
            results[rig.name] = "applied"
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

@router.get("/oc-profiles")
def get_oc_profiles():
    db = get_db()
    return [_oc_to_dict(p) for p in OcProfile.get_all(db)]


def _oc_to_dict(p: OcProfile) -> dict:
    return {
        "name": p.name, "core_offset": p.core_offset, "mem_offset": p.mem_offset,
        "power_limit": p.power_limit, "fan_speed": p.fan_speed,
        "per_gpu": p.per_gpu,
    }


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
             "gpu_type": m.gpu_type, "algos": m.supported_algos}
            for m in list_miners()]
