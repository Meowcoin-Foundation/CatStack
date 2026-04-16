from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Rig:
    id: int | None = None
    name: str = ""
    host: str = ""
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_key_path: str | None = None
    group_id: int | None = None
    flight_sheet_id: int | None = None
    oc_profile_id: int | None = None
    agent_version: str | None = None
    os_info: str | None = None
    gpu_list: str | None = None  # JSON string
    cpu_model: str | None = None
    notes: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    # Joined fields (not stored directly)
    group_name: str | None = None
    flight_sheet_name: str | None = None
    oc_profile_name: str | None = None

    @property
    def gpu_names(self) -> list[str]:
        if not self.gpu_list:
            return []
        try:
            return json.loads(self.gpu_list)
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def from_row(row: sqlite3.Row) -> Rig:
        keys = row.keys()
        return Rig(**{k: row[k] for k in keys if k in Rig.__dataclass_fields__})

    def save(self, db: sqlite3.Connection) -> Rig:
        if self.id is None:
            cur = db.execute(
                """INSERT INTO rigs (name, host, ssh_port, ssh_user, ssh_key_path,
                   group_id, flight_sheet_id, oc_profile_id, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.name, self.host, self.ssh_port, self.ssh_user,
                 self.ssh_key_path, self.group_id, self.flight_sheet_id,
                 self.oc_profile_id, self.notes),
            )
            self.id = cur.lastrowid
        else:
            db.execute(
                """UPDATE rigs SET name=?, host=?, ssh_port=?, ssh_user=?,
                   ssh_key_path=?, group_id=?, flight_sheet_id=?, oc_profile_id=?,
                   notes=?, updated_at=datetime('now') WHERE id=?""",
                (self.name, self.host, self.ssh_port, self.ssh_user,
                 self.ssh_key_path, self.group_id, self.flight_sheet_id,
                 self.oc_profile_id, self.notes, self.id),
            )
        db.commit()
        return self

    def delete(self, db: sqlite3.Connection):
        if self.id is not None:
            db.execute("DELETE FROM rigs WHERE id=?", (self.id,))
            db.commit()

    @staticmethod
    def get_by_name(db: sqlite3.Connection, name: str) -> Rig | None:
        row = db.execute(
            """SELECT r.*, g.name as group_name,
                      fs.name as flight_sheet_name,
                      oc.name as oc_profile_name
               FROM rigs r
               LEFT JOIN groups g ON r.group_id = g.id
               LEFT JOIN flight_sheets fs ON r.flight_sheet_id = fs.id
               LEFT JOIN oc_profiles oc ON r.oc_profile_id = oc.id
               WHERE r.name = ?""",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return Rig.from_row(row)

    @staticmethod
    def get_all(db: sqlite3.Connection, group_name: str | None = None) -> list[Rig]:
        if group_name:
            rows = db.execute(
                """SELECT r.*, g.name as group_name,
                          fs.name as flight_sheet_name,
                          oc.name as oc_profile_name
                   FROM rigs r
                   LEFT JOIN groups g ON r.group_id = g.id
                   LEFT JOIN flight_sheets fs ON r.flight_sheet_id = fs.id
                   LEFT JOIN oc_profiles oc ON r.oc_profile_id = oc.id
                   WHERE g.name = ?
                   ORDER BY r.name""",
                (group_name,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT r.*, g.name as group_name,
                          fs.name as flight_sheet_name,
                          oc.name as oc_profile_name
                   FROM rigs r
                   LEFT JOIN groups g ON r.group_id = g.id
                   LEFT JOIN flight_sheets fs ON r.flight_sheet_id = fs.id
                   LEFT JOIN oc_profiles oc ON r.oc_profile_id = oc.id
                   ORDER BY r.name""",
            ).fetchall()
        return [Rig.from_row(r) for r in rows]


@dataclass
class Group:
    id: int | None = None
    name: str = ""
    notes: str | None = None
    created_at: str | None = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> Group:
        keys = row.keys()
        return Group(**{k: row[k] for k in keys if k in Group.__dataclass_fields__})

    def save(self, db: sqlite3.Connection) -> Group:
        if self.id is None:
            cur = db.execute(
                "INSERT INTO groups (name, notes) VALUES (?, ?)",
                (self.name, self.notes),
            )
            self.id = cur.lastrowid
        else:
            db.execute(
                "UPDATE groups SET name=?, notes=? WHERE id=?",
                (self.name, self.notes, self.id),
            )
        db.commit()
        return self

    def delete(self, db: sqlite3.Connection):
        if self.id is not None:
            db.execute("UPDATE rigs SET group_id=NULL WHERE group_id=?", (self.id,))
            db.execute("DELETE FROM groups WHERE id=?", (self.id,))
            db.commit()

    @staticmethod
    def get_by_name(db: sqlite3.Connection, name: str) -> Group | None:
        row = db.execute("SELECT * FROM groups WHERE name=?", (name,)).fetchone()
        return Group.from_row(row) if row else None

    @staticmethod
    def get_all(db: sqlite3.Connection) -> list[Group]:
        rows = db.execute("SELECT * FROM groups ORDER BY name").fetchall()
        return [Group.from_row(r) for r in rows]


@dataclass
class FlightSheet:
    id: int | None = None
    name: str = ""
    coin: str = ""
    algo: str = ""
    miner: str = ""
    miner_version: str | None = None
    pool_url: str = ""
    pool_url2: str | None = None
    wallet: str = ""
    worker_template: str = "%HOSTNAME%"
    password: str = "x"
    extra_args: str = ""
    is_solo: int = 0
    solo_rpc_user: str | None = None
    solo_rpc_pass: str | None = None
    coinbase_addr: str | None = None
    notes: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> FlightSheet:
        keys = row.keys()
        return FlightSheet(**{k: row[k] for k in keys if k in FlightSheet.__dataclass_fields__})

    def save(self, db: sqlite3.Connection) -> FlightSheet:
        if self.id is None:
            cur = db.execute(
                """INSERT INTO flight_sheets
                   (name, coin, algo, miner, miner_version, pool_url, pool_url2,
                    wallet, worker_template, password, extra_args, is_solo,
                    solo_rpc_user, solo_rpc_pass, coinbase_addr, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.name, self.coin, self.algo, self.miner, self.miner_version,
                 self.pool_url, self.pool_url2, self.wallet, self.worker_template,
                 self.password, self.extra_args, self.is_solo, self.solo_rpc_user,
                 self.solo_rpc_pass, self.coinbase_addr, self.notes),
            )
            self.id = cur.lastrowid
        else:
            db.execute(
                """UPDATE flight_sheets SET name=?, coin=?, algo=?, miner=?,
                   miner_version=?, pool_url=?, pool_url2=?, wallet=?,
                   worker_template=?, password=?, extra_args=?, is_solo=?,
                   solo_rpc_user=?, solo_rpc_pass=?, coinbase_addr=?, notes=?,
                   updated_at=datetime('now') WHERE id=?""",
                (self.name, self.coin, self.algo, self.miner, self.miner_version,
                 self.pool_url, self.pool_url2, self.wallet, self.worker_template,
                 self.password, self.extra_args, self.is_solo, self.solo_rpc_user,
                 self.solo_rpc_pass, self.coinbase_addr, self.notes, self.id),
            )
        db.commit()
        return self

    def delete(self, db: sqlite3.Connection):
        if self.id is not None:
            db.execute("UPDATE rigs SET flight_sheet_id=NULL WHERE flight_sheet_id=?", (self.id,))
            db.execute("DELETE FROM flight_sheets WHERE id=?", (self.id,))
            db.commit()

    @staticmethod
    def get_by_name(db: sqlite3.Connection, name: str) -> FlightSheet | None:
        row = db.execute("SELECT * FROM flight_sheets WHERE name=?", (name,)).fetchone()
        return FlightSheet.from_row(row) if row else None

    @staticmethod
    def get_all(db: sqlite3.Connection) -> list[FlightSheet]:
        rows = db.execute("SELECT * FROM flight_sheets ORDER BY name").fetchall()
        return [FlightSheet.from_row(r) for r in rows]


@dataclass
class OcProfile:
    id: int | None = None
    name: str = ""
    core_offset: int | None = None
    mem_offset: int | None = None
    core_lock: int | None = None
    mem_lock: int | None = None
    power_limit: int | None = None
    fan_speed: int | None = None
    per_gpu_overrides: str | None = None  # JSON
    amd_core_state: str | None = None
    amd_mem_state: str | None = None
    amd_voltage: int | None = None
    notes: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def per_gpu(self) -> list[dict] | None:
        if not self.per_gpu_overrides:
            return None
        try:
            return json.loads(self.per_gpu_overrides)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def from_row(row: sqlite3.Row) -> OcProfile:
        keys = row.keys()
        return OcProfile(**{k: row[k] for k in keys if k in OcProfile.__dataclass_fields__})

    def save(self, db: sqlite3.Connection) -> OcProfile:
        if self.id is None:
            cur = db.execute(
                """INSERT INTO oc_profiles
                   (name, core_offset, mem_offset, core_lock, mem_lock,
                    power_limit, fan_speed,
                    per_gpu_overrides, amd_core_state, amd_mem_state, amd_voltage, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.name, self.core_offset, self.mem_offset,
                 self.core_lock, self.mem_lock,
                 self.power_limit, self.fan_speed, self.per_gpu_overrides,
                 self.amd_core_state, self.amd_mem_state, self.amd_voltage, self.notes),
            )
            self.id = cur.lastrowid
        else:
            db.execute(
                """UPDATE oc_profiles SET name=?, core_offset=?, mem_offset=?,
                   core_lock=?, mem_lock=?,
                   power_limit=?, fan_speed=?, per_gpu_overrides=?,
                   amd_core_state=?, amd_mem_state=?, amd_voltage=?, notes=?,
                   updated_at=datetime('now') WHERE id=?""",
                (self.name, self.core_offset, self.mem_offset,
                 self.core_lock, self.mem_lock,
                 self.power_limit, self.fan_speed, self.per_gpu_overrides,
                 self.amd_core_state, self.amd_mem_state, self.amd_voltage, self.notes, self.id),
            )
        db.commit()
        return self

    def delete(self, db: sqlite3.Connection):
        if self.id is not None:
            db.execute("UPDATE rigs SET oc_profile_id=NULL WHERE oc_profile_id=?", (self.id,))
            db.execute("DELETE FROM oc_profiles WHERE id=?", (self.id,))
            db.commit()

    @staticmethod
    def get_by_name(db: sqlite3.Connection, name: str) -> OcProfile | None:
        row = db.execute("SELECT * FROM oc_profiles WHERE name=?", (name,)).fetchone()
        return OcProfile.from_row(row) if row else None

    @staticmethod
    def get_all(db: sqlite3.Connection) -> list[OcProfile]:
        rows = db.execute("SELECT * FROM oc_profiles ORDER BY name").fetchall()
        return [OcProfile.from_row(r) for r in rows]
