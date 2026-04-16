#!/bin/bash
#
# MFarm First-Boot Provisioning Script
# Runs once on first boot after Ubuntu install to set up NVIDIA drivers and finalize MFarm.
# After completion, this service disables itself.
#
set -euo pipefail

LOG="/var/log/mfarm/firstboot.log"
MARKER="/opt/mfarm/.firstboot-done"

exec > >(tee -a "$LOG") 2>&1

echo "============================================"
echo "  MFarm First-Boot Provisioning"
echo "  $(date)"
echo "  Hostname: $(hostname)"
echo "============================================"
echo ""

# Skip if already done
if [[ -f "$MARKER" ]]; then
    echo "First-boot already completed. Exiting."
    systemctl disable mfarm-firstboot.service
    exit 0
fi

# ── 1. Wait for network ──────────────────────────────────────────────

echo "[1/7] Waiting for network..."
for i in $(seq 1 30); do
    if ping -c 1 -W 2 8.8.8.8 &>/dev/null; then
        echo "  Network is up"
        break
    fi
    echo "  Waiting... ($i/30)"
    sleep 2
done

# ── 2. Update package lists ──────────────────────────────────────────

echo "[2/7] Updating package lists..."
apt-get update -qq

# ── 3. Install NVIDIA drivers ────────────────────────────────────────

echo "[3/7] Installing NVIDIA drivers..."

# Detect if we have NVIDIA GPUs
if lspci | grep -qi nvidia; then
    echo "  NVIDIA GPU(s) detected"

    # Add NVIDIA driver PPA
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:graphics-drivers/ppa
    apt-get update -qq

    # Install the recommended driver
    # Use ubuntu-drivers to detect and install
    apt-get install -y -qq ubuntu-drivers-common
    RECOMMENDED=$(ubuntu-drivers devices 2>/dev/null | grep "recommended" | head -1 | awk '{print $3}')

    if [[ -n "$RECOMMENDED" ]]; then
        echo "  Installing recommended driver: $RECOMMENDED"
        apt-get install -y -qq "$RECOMMENDED"
    else
        # Fallback to a known good driver
        echo "  No recommended driver found, installing nvidia-driver-535"
        apt-get install -y -qq nvidia-driver-535
    fi

    # Install CUDA toolkit (needed for some miners)
    echo "  Installing CUDA toolkit..."
    apt-get install -y -qq nvidia-cuda-toolkit || true

    # Enable persistence mode
    nvidia-smi -pm 1 2>/dev/null || true

    # Set up nvidia-persistenced
    systemctl enable nvidia-persistenced 2>/dev/null || true

    echo "  NVIDIA driver installed: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null || echo 'will be active after reboot')"
else
    echo "  No NVIDIA GPU detected, skipping driver install"
fi

# ── 4. Install lm-sensors and detect ─────────────────────────────────

echo "[4/7] Configuring sensors..."
sensors-detect --auto >/dev/null 2>&1 || true

# ── 5. Set unique hostname based on MAC address ─────────────────────

echo "[5/7] Setting hostname..."
# Get the MAC of the first ethernet interface (last 4 chars for uniqueness)
MAC_SUFFIX=$(ip link show | grep -m1 "link/ether" | awk '{print $2}' | tr -d ':' | tail -c 5)
NEW_HOSTNAME="mfarm-rig-${MAC_SUFFIX}"
hostnamectl set-hostname "$NEW_HOSTNAME"
echo "  Hostname set to: $NEW_HOSTNAME"

# Update /etc/hosts
sed -i "s/mfarm-rig/$NEW_HOSTNAME/g" /etc/hosts

# ── 6. Collect hardware info ─────────────────────────────────────────

echo "[6/7] Collecting hardware info..."
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

import socket
hw['hostname'] = socket.gethostname()

with open('/var/run/mfarm/hwinfo.json', 'w') as f:
    json.dump(hw, f, indent=2)
print(json.dumps(hw, indent=2))
"

# ── 7. Enable and start MFarm agent ─────────────────────────────────

echo "[7/7] Enabling MFarm agent..."
systemctl enable mfarm-agent
systemctl start mfarm-agent

# Verify
sleep 3
if systemctl is-active --quiet mfarm-agent; then
    echo "  MFarm agent is running"
else
    echo "  WARNING: MFarm agent failed to start, check: journalctl -u mfarm-agent"
fi

# ── Done ─────────────────────────────────────────────────────────────

touch "$MARKER"
systemctl disable mfarm-firstboot.service

echo ""
echo "============================================"
echo "  MFarm First-Boot Complete!"
echo "  Hostname: $(hostname)"
echo "  IP: $(hostname -I | awk '{print $1}')"
echo ""
echo "  From your Windows console, add this rig:"
echo "    mfarm rig add $(hostname) $(hostname -I | awk '{print $1}')"
echo ""
echo "  A reboot is recommended for NVIDIA drivers."
echo "  Rebooting in 10 seconds..."
echo "============================================"

sleep 10
reboot
