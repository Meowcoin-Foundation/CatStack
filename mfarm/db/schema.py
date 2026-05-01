import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

TABLES = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS flight_sheets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    coin            TEXT NOT NULL,
    algo            TEXT NOT NULL,
    miner           TEXT NOT NULL,
    miner_version   TEXT,
    pool_url        TEXT NOT NULL,
    pool_url2       TEXT,
    wallet          TEXT NOT NULL,
    worker_template TEXT NOT NULL DEFAULT '%HOSTNAME%',
    password        TEXT DEFAULT 'x',
    extra_args      TEXT DEFAULT '',
    is_solo         INTEGER NOT NULL DEFAULT 0,
    solo_rpc_user   TEXT,
    solo_rpc_pass   TEXT,
    coinbase_addr   TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oc_profiles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    core_offset         INTEGER,
    mem_offset          INTEGER,
    core_lock           INTEGER,
    mem_lock            INTEGER,
    power_limit         INTEGER,
    fan_speed           INTEGER,
    per_gpu_overrides   TEXT,
    amd_core_state      TEXT,
    amd_mem_state       TEXT,
    amd_voltage         INTEGER,
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rigs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    host            TEXT NOT NULL,
    ssh_port        INTEGER NOT NULL DEFAULT 22,
    ssh_user        TEXT NOT NULL DEFAULT 'root',
    ssh_key_path    TEXT,
    group_id        INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    flight_sheet_id INTEGER REFERENCES flight_sheets(id) ON DELETE SET NULL,
    oc_profile_id   INTEGER REFERENCES oc_profiles(id) ON DELETE SET NULL,
    agent_version   TEXT,
    os_info         TEXT,
    gpu_list        TEXT,
    cpu_model       TEXT,
    mac             TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS miners (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    binary_path     TEXT,
    download_url    TEXT,
    algos           TEXT,
    gpu_type        TEXT DEFAULT 'nvidia',
    api_type        TEXT NOT NULL,
    api_port        INTEGER DEFAULT 4068,
    notes           TEXT,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS rig_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rig_id      INTEGER NOT NULL REFERENCES rigs(id) ON DELETE CASCADE,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    hashrate    REAL,
    power_draw  REAL,
    gpu_temps   TEXT,
    gpu_fans    TEXT,
    accepted    INTEGER,
    rejected    INTEGER,
    uptime_secs INTEGER
);

CREATE INDEX IF NOT EXISTS idx_snapshots_rig_time ON rig_snapshots(rig_id, timestamp);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rig_id      INTEGER REFERENCES rigs(id) ON DELETE CASCADE,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    message     TEXT NOT NULL,
    details     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_rig_time ON events(rig_id, timestamp);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(TABLES)
    # Idempotent column additions for DBs created before the column existed.
    # CREATE TABLE IF NOT EXISTS doesn't alter existing tables, so any column
    # added after the initial schema needs an explicit ALTER guarded against
    # duplicate-column errors.
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(rigs)").fetchall()}
    if "mac" not in existing_cols:
        conn.execute("ALTER TABLE rigs ADD COLUMN mac TEXT")
        conn.commit()
    # Track schema version
    existing = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    if existing is None or existing < SCHEMA_VERSION:
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    return conn
