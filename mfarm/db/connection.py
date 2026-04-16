import sqlite3
import threading
from pathlib import Path

from mfarm.config import DB_PATH, ensure_app_dir
from mfarm.db.schema import init_db

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                ensure_app_dir()
                _conn = init_db(DB_PATH)
    return _conn


def close_db():
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
