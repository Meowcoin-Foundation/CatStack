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

# 1a. Unmask any of our units that may have been masked by a prior
# install-vast.sh run (it masks these as part of Vast host setup). Without
# this, masked units exist as symlinks to /dev/null in /etc/systemd/system,
# and `cp foo.service /etc/systemd/system/foo.service` follows the symlink
# and writes the unit content into /dev/null. Unmasking removes the symlink
# so the subsequent cp writes a real file.
#
# A Vast.ai host (machine_id present) intentionally has these masked — Vast
# workloads don't want a mining agent or broadcast service running. Skip
# the unmask in that case so we don't accidentally re-enable mining on a
# Vast host the operator has converted.
IS_VAST_HOST=0
[[ -f /var/lib/vastai_kaalia/machine_id ]] && IS_VAST_HOST=1
if [[ "$IS_VAST_HOST" == "0" ]]; then
    for svc in mfarm-agent meowos-phonehome meowos-webui meowos-xmrig meowos-updater; do
        systemctl unmask "${svc}.service" 2>/dev/null || true
    done
    systemctl unmask meowos-updater.timer 2>/dev/null || true
else
    echo "  Vast host detected — leaving service masks in place"
fi

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

# 5a. Install auto-updater (timer + service) so this rig pulls future agent
# bundles from the console without needing another SSH push. Idempotent —
# overwrites are fine, no-op if already installed.
if [[ -f /tmp/mfarm-deploy/meowos-updater.py ]]; then
    cp /tmp/mfarm-deploy/meowos-updater.py "$INSTALL_DIR/meowos-updater.py"
    chmod +x "$INSTALL_DIR/meowos-updater.py"
    cp /tmp/mfarm-deploy/meowos-updater.service /etc/systemd/system/meowos-updater.service
    cp /tmp/mfarm-deploy/meowos-updater.timer /etc/systemd/system/meowos-updater.timer
    # Stamp the rig with this VERSION so the updater has a baseline to compare
    if [[ -f /tmp/mfarm-deploy/VERSION ]]; then
        cp /tmp/mfarm-deploy/VERSION "$INSTALL_DIR/VERSION"
    fi
fi

# 5c. Install phonehome service. Required for two console-side features:
#   - cross-subnet console discovery (rig writes /var/run/mfarm/console_url
#     after the console replies to its UDP broadcast)
#   - rig auto-heal: console matches phonehome MAC against Rig.mac and
#     auto-updates Rig.host when DHCP shifts the rig's IP.
# Older rigs deployed via SSH never had this, so flashed-image features
# silently didn't apply to them — pushing it via deploy.sh closes that gap.
if [[ -f /tmp/mfarm-deploy/meowos-phonehome.py ]]; then
    cp /tmp/mfarm-deploy/meowos-phonehome.py "$INSTALL_DIR/meowos-phonehome.py"
    chmod +x "$INSTALL_DIR/meowos-phonehome.py"
    cp /tmp/mfarm-deploy/meowos-phonehome.service /etc/systemd/system/meowos-phonehome.service
fi

# 5b. Install 1GB hugepages allocator for XMRig RandomX (CPU-mining rigs).
# RandomX dataset is 2080 MB + 256 MB cache = needs 3 x 1GB pages. Allocating
# them after boot is unreliable (memory fragmentation) — must run BEFORE
# mfarm-agent.service starts the miner. Cost on non-CPU-mining rigs is zero
# (xmrig won't be the configured miner so the pages just sit unused, freeable
# via `echo 0 > .../nr_hugepages`).
if [[ -f /tmp/mfarm-deploy/xmrig-1gb-hugepages.service ]]; then
    cp /tmp/mfarm-deploy/xmrig-1gb-hugepages.service /etc/systemd/system/xmrig-1gb-hugepages.service
    # Only enable if the CPU supports 1GB pages (`pdpe1gb` flag in /proc/cpuinfo).
    # Without that flag the service would just no-op.
    if grep -q pdpe1gb /proc/cpuinfo; then
        systemctl enable xmrig-1gb-hugepages.service 2>/dev/null || true
        echo "  1GB hugepages service enabled (CPU supports pdpe1gb)"
    else
        echo "  CPU lacks pdpe1gb — 1GB hugepages service installed but not enabled"
    fi
fi

# 6. Write initial config if none exists
echo "[5/8] Checking config..."
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
    cat > "$CONFIG_DIR/config.json" << 'CFG'
{
    "agent": {
        "version": "0.1.0",
        "stats_interval": 2,
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

# Enable updater timer (idempotent). If it fails, the rig is unchanged — the
# operator can re-deploy or fix manually.
if [[ -f /etc/systemd/system/meowos-updater.timer ]]; then
    systemctl enable --now meowos-updater.timer 2>/dev/null || true
fi

# Enable phonehome (idempotent). Restart so any code update (e.g. the
# UDP-reply listener for cross-subnet discovery) takes effect. Skip on
# Vast hosts — they intentionally keep this masked.
if [[ "$IS_VAST_HOST" == "0" ]] && [[ -f /etc/systemd/system/meowos-phonehome.service ]]; then
    systemctl enable meowos-phonehome.service 2>/dev/null || true
    systemctl restart meowos-phonehome.service 2>/dev/null || true
fi

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
