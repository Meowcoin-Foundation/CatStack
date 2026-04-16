import os
from pathlib import Path

APP_NAME = "mfarm"
APP_DIR = Path(os.environ.get("MFARM_HOME", Path.home() / ".mfarm"))
DB_PATH = APP_DIR / "mfarm.db"
SSH_KEY_DEFAULT = Path.home() / ".ssh" / "id_rsa"

# SSH defaults
SSH_CONNECT_TIMEOUT = 10
SSH_COMMAND_TIMEOUT = 30
SSH_KEEPALIVE_INTERVAL = 30
SSH_MAX_WORKERS = 10

# Dashboard defaults
DASHBOARD_REFRESH_INTERVAL = 5

# Agent defaults
AGENT_STATS_INTERVAL = 5
AGENT_WATCHDOG_INTERVAL = 30
AGENT_MAX_GPU_TEMP = 90
AGENT_CRITICAL_GPU_TEMP = 95
AGENT_MAX_RESTARTS = 5
AGENT_RESTART_WINDOW = 600

# Rig paths
RIG_INSTALL_DIR = "/opt/mfarm"
RIG_CONFIG_DIR = "/etc/mfarm"
RIG_LOG_DIR = "/var/log/mfarm"
RIG_RUN_DIR = "/var/run/mfarm"
RIG_MINER_DIR = "/opt/mfarm/miners"

def ensure_app_dir():
    APP_DIR.mkdir(parents=True, exist_ok=True)
