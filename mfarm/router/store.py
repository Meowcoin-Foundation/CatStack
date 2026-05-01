"""Persistence for router-integration config. Single-row table; we read+write
the JSON blob in `config` and the chosen backend name."""

from __future__ import annotations

import json
import logging
import sqlite3

from mfarm.router import BACKENDS, get_backend
from mfarm.router.base import ConfigError, RouterBackend

log = logging.getLogger(__name__)


def load_config(db: sqlite3.Connection) -> tuple[str, dict]:
    row = db.execute(
        "SELECT backend, config FROM router_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return ("manual", {})
    backend = row[0] or "manual"
    try:
        config = json.loads(row[1] or "{}")
    except json.JSONDecodeError:
        config = {}
    return (backend, config)


def save_config(db: sqlite3.Connection, backend: str, config: dict) -> None:
    if backend not in BACKENDS:
        raise ConfigError(f"unknown backend: {backend}")
    # Validate before persisting so the dashboard doesn't accept obviously
    # broken creds.
    inst = get_backend(backend, config)
    inst.validate_config()
    db.execute(
        "UPDATE router_config SET backend = ?, config = ?, "
        "updated_at = datetime('now') WHERE id = 1",
        (backend, json.dumps(config)),
    )
    db.commit()


def current_backend(db: sqlite3.Connection) -> RouterBackend:
    """Return an instance of the currently-configured backend.
    Falls back to manual if config is malformed."""
    backend, config = load_config(db)
    try:
        return get_backend(backend, config)
    except KeyError:
        log.warning("router_config has unknown backend '%s'; falling back to manual", backend)
        return get_backend("manual", {})
