#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/mfarm"
CONFIG_DIR="/etc/mfarm"
LOG_DIR="/var/log/mfarm"
RUN_DIR="/var/run/mfarm"
MINER_DIR="/opt/mfarm/miners"

echo "=== MeowFarm Agent Deployment ==="
echo "  Host: $(hostname)"
echo "  Date: $(date)"
echo ""

# 1. Create directory structure
echo "[1/8] Creating directories..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" "$RUN_DIR" "$MINER_DIR"

# 2. Ensure Python3
echo "[2/8] Checking Python3..."
if ! command -v python3 &>/dev/null; then
    echo "  Installing Python3..."
    apt-get update -qq && apt-get install -y -qq python3
fi
echo "  Python3: $(python3 --version)"

# 3. Copy agent
echo "[3/8] Installing agent..."
cp /tmp/mfarm-deploy/mfarm-agent.py "$INSTALL_DIR/mfarm-agent.py"
chmod +x "$INSTALL_DIR/mfarm-agent.py"

# 4. Copy miner wrapper
if [[ -f /tmp/mfarm-deploy/miner-wrapper.sh ]]; then
    cp /tmp/mfarm-deploy/miner-wrapper.sh "$INSTALL_DIR/miner-wrapper.sh"
    chmod +x "$INSTALL_DIR/miner-wrapper.sh"
fi

# 4b. Install `miner` shell function (system-wide profile.d) so SSH'ing in and
# typing `miner` shows the running miner output, plus `miner start|stop|restart`
# control. Sourced by both interactive and login bash. Idempotent.
if [[ -f /tmp/mfarm-deploy/miner-attach.sh ]]; then
    cp /tmp/mfarm-deploy/miner-attach.sh /etc/profile.d/miner-attach.sh
    chmod 644 /etc/profile.d/miner-attach.sh
    # Defensive: some bashrc setups skip /etc/profile.d when invoked as
    # non-login shells. Source it from /root/.bashrc explicitly too.
    if ! grep -q miner-attach /root/.bashrc 2>/dev/null; then
        echo '. /etc/profile.d/miner-attach.sh' >> /root/.bashrc
    fi
fi

# 5. Install systemd service
echo "[4/8] Installing systemd service..."
cp /tmp/mfarm-deploy/mfarm-agent.service /etc/systemd/system/mfarm-agent.service

# 6. Write initial config if none exists
echo "[5/8] Checking config..."
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
    cat > "$CONFIG_DIR/config.json" << 'CFG'
{
    "agent": {
        "version": "0.1.0",
        "stats_interval": 5,
        "watchdog_interval": 30,
        "max_gpu_temp": 90,
        "critical_gpu_temp": 95,
        "max_restarts_per_window": 5,
        "restart_window_secs": 600
    },
    "flight_sheet": null,
    "oc_profile": null,
    "miner_paths": {},
    "api_ports": {
        "ccminer": 4068,
        "trex": 4067,
        "lolminer": 44444,
        "cpuminer-opt": 4048,
        "xmrig": 44445
    }
}
CFG
    echo "  Created default config"
else
    echo "  Existing config preserved"
fi

# 7. Set up log rotation
echo "[6/8] Configuring log rotation..."
cat > /etc/logrotate.d/mfarm << 'LR'
/var/log/mfarm/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    size 50M
}
LR

# 8. Collect hardware info
echo "[7/8] Collecting hardware info..."
python3 -c "
import json, subprocess, os

hw = {}

# GPU info (NVIDIA)
try:
    r = subprocess.run(['nvidia-smi', '--query-gpu=name,pci.bus_id,memory.total',
                        '--format=csv,noheader,nounits'],
                       capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        gpus = []
        for line in r.stdout.strip().split('\n'):
            if line.strip():
                parts = [p.strip() for p in line.split(',')]
                gpus.append({'name': parts[0], 'pci_bus': parts[1], 'vram_mb': int(parts[2])})
        hw['gpus'] = gpus
        hw['gpu_vendor'] = 'nvidia'
except Exception:
    hw['gpus'] = []

# GPU info (AMD fallback)
if not hw.get('gpus'):
    try:
        import glob
        cards = sorted(glob.glob('/sys/class/drm/card[0-9]*/device/product_name'))
        gpus = []
        for c in cards:
            try:
                name = open(c).read().strip()
                gpus.append({'name': name})
            except:
                pass
        if gpus:
            hw['gpus'] = gpus
            hw['gpu_vendor'] = 'amd'
    except:
        pass

# CPU
try:
    with open('/proc/cpuinfo') as f:
        for line in f:
            if 'model name' in line:
                hw['cpu_model'] = line.split(':')[1].strip()
                break
    hw['cpu_cores'] = os.cpu_count()
except:
    pass

# RAM
try:
    with open('/proc/meminfo') as f:
        for line in f:
            if 'MemTotal' in line:
                hw['mem_total_kb'] = int(line.split()[1])
                break
except:
    pass

# OS
try:
    with open('/etc/os-release') as f:
        for line in f:
            if line.startswith('PRETTY_NAME='):
                hw['os'] = line.split('=',1)[1].strip().strip('\"')
                break
except:
    pass

# HiveOS version
try:
    with open('/hive/etc/VERSION') as f:
        hw['hiveos_version'] = f.read().strip()
except:
    pass

with open('/var/run/mfarm/hwinfo.json', 'w') as f:
    json.dump(hw, f, indent=2)
print(json.dumps(hw, indent=2))
"

# 9. Disable HiveOS agent if present
if systemctl is-active --quiet hive-agent 2>/dev/null; then
    echo ""
    echo "  [!] HiveOS agent detected and running."
    if [[ "${DISABLE_HIVEOS:-0}" == "1" ]]; then
        echo "  Disabling hive-agent..."
        systemctl stop hive-agent
        systemctl disable hive-agent
        echo "  HiveOS agent disabled. To re-enable: systemctl enable --now hive-agent"
    else
        echo "  To disable it, re-run with DISABLE_HIVEOS=1 or run:"
        echo "    systemctl stop hive-agent && systemctl disable hive-agent"
    fi
fi

# 10. Enable and start
echo "[8/8] Starting agent..."
systemctl daemon-reload
systemctl enable mfarm-agent
systemctl restart mfarm-agent

# Wait briefly for startup
sleep 2
STATUS=$(systemctl is-active mfarm-agent)

echo ""
echo "========================================"
echo "  MeowFarm Agent Installed Successfully"
echo "========================================"
echo "  Status:  $STATUS"
echo "  Logs:    journalctl -u mfarm-agent -f"
echo "  Config:  $CONFIG_DIR/config.json"
echo "  Stats:   cat $RUN_DIR/stats.json"
echo ""

# Cleanup
rm -rf /tmp/mfarm-deploy
