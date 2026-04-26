#!/bin/bash
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive

# Auto-detect the Crucial SSD (~931G, not the WSL root)
DISK=$(lsblk -nd -o NAME,SIZE,MOUNTPOINT | grep -v '/' | awk '$2 ~ /931/ {print "/dev/"$1}' | head -1)
if [ -z "$DISK" ]; then echo "ERROR: Can't find Crucial SSD"; lsblk; exit 1; fi
echo "Found SSD at: $DISK"
MNT="/tmp/mfarm-rootfs"
MFARM="/mnt/c/Source/mfarm/build-usb/mfarm-files"

echo "[1/5] Writing configs..."
ROOT_UUID=$(blkid -s UUID -o value "${DISK}2")
EFI_UUID=$(blkid -s UUID -o value "${DISK}1")

cat > "$MNT/etc/fstab" <<EOF
UUID=$ROOT_UUID   /          ext4   errors=remount-ro   0 1
UUID=$EFI_UUID    /boot/efi  vfat   umask=0077          0 1
EOF

cat > "$MNT/etc/apt/sources.list" <<'EOF'
deb http://us.archive.ubuntu.com/ubuntu jammy main restricted universe multiverse
deb http://us.archive.ubuntu.com/ubuntu jammy-updates main restricted universe multiverse
deb http://us.archive.ubuntu.com/ubuntu jammy-security main restricted universe multiverse
EOF

echo "mfarm-rig" > "$MNT/etc/hostname"
printf "127.0.0.1\tlocalhost\n127.0.1.1\tmfarm-rig\n" > "$MNT/etc/hosts"
ln -sf /usr/share/zoneinfo/UTC "$MNT/etc/localtime"

mkdir -p "$MNT/etc/netplan"
cat > "$MNT/etc/netplan/01-mfarm.yaml" <<'EOF'
network:
  version: 2
  renderer: networkd
  ethernets:
    all-en:
      match:
        name: "en*"
      dhcp4: true
    all-eth:
      match:
        name: "eth*"
      dhcp4: true
    all-other:
      match:
        driver: "*"
      dhcp4: true
      optional: true
EOF

rm -f "$MNT/etc/resolv.conf"
printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" > "$MNT/etc/resolv.conf"
mkdir -p "$MNT/etc/tmpfiles.d"
echo "d /var/run/mfarm 0755 root root -" > "$MNT/etc/tmpfiles.d/mfarm.conf"
echo "[1/5] Done"

echo "[2/5] Installing kernel + packages (~3 min)..."
mount --bind /dev "$MNT/dev" 2>/dev/null || true
mount --bind /dev/pts "$MNT/dev/pts" 2>/dev/null || true
mount -t proc proc "$MNT/proc" 2>/dev/null || true
mount -t sysfs sys "$MNT/sys" 2>/dev/null || true

cat > "$MNT/tmp/setup.sh" <<'SETUP'
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export HOME=/root
apt-get update -qq
apt-get install -y -qq \
    linux-image-generic linux-headers-generic \
    grub-efi-amd64 systemd-sysv \
    openssh-server python3 python3-venv \
    lm-sensors htop screen wget curl \
    net-tools pciutils usbutils \
    smartmontools sysstat nvme-cli \
    dkms build-essential \
    software-properties-common ubuntu-drivers-common \
    sudo locales iproute2 iputils-ping netplan.io \
    xserver-xorg-core xinit x11-xserver-utils
locale-gen en_US.UTF-8
useradd -m -s /bin/bash -G sudo,video miner
echo "miner:mfarm" | chpasswd
echo "root:mfarm" | chpasswd
echo 'miner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/miner
chmod 440 /etc/sudoers.d/miner
mkdir -p /home/miner/.ssh
chmod 700 /home/miner/.ssh
chown miner:miner /home/miner/.ssh
sed -i 's/#PermitRootLogin .*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/#PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config
systemctl enable ssh
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
cat > /etc/rc.local <<'RC'
#!/bin/bash
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance > "$cpu" 2>/dev/null
done
nvidia-smi --ecc-config=0 2>/dev/null || true
nvidia-smi -pm 1 2>/dev/null || true
exit 0
RC
chmod +x /etc/rc.local
printf '*    soft    nofile    65535\n*    hard    nofile    65535\n' >> /etc/security/limits.conf
cat > /etc/sysctl.d/99-mfarm.conf <<'SYSCTL'
vm.swappiness=10
net.core.somaxconn=65535
kernel.panic=10
kernel.panic_on_oops=1
SYSCTL
systemctl enable systemd-networkd

# Auto-login on tty1 (no monitor needed but if one is connected, no login prompt)
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/override.conf <<'AUTOLOGIN'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin miner --noclear %I $TERM
AUTOLOGIN

# Brand as MeowOS
sed -i 's/PRETTY_NAME=.*/PRETTY_NAME="MeowOS 1.0"/' /etc/os-release
echo "CHROOT_SETUP_DONE"
SETUP
chmod +x "$MNT/tmp/setup.sh"
chroot "$MNT" /tmp/setup.sh
echo "[2/5] Done"

echo "[3/5] SSH keys + MeowFarm agent + ccminer..."
PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIETCncNMggVWmhKhO8ylpK2g8/czRm6TeKOEDrga8MVr benefit14snake@hotmail.com"
echo "$PUBKEY" > "$MNT/home/miner/.ssh/authorized_keys"
chmod 600 "$MNT/home/miner/.ssh/authorized_keys"
chroot "$MNT" chown miner:miner /home/miner/.ssh/authorized_keys
mkdir -p "$MNT/root/.ssh"
echo "$PUBKEY" > "$MNT/root/.ssh/authorized_keys"
chmod 600 "$MNT/root/.ssh/authorized_keys"
chmod 700 "$MNT/root/.ssh"

mkdir -p "$MNT/opt/mfarm/miners" "$MNT/etc/mfarm" "$MNT/var/log/mfarm" "$MNT/var/run/mfarm"
cp /mnt/c/Source/mfarm/mfarm/worker/mfarm-agent.py "$MNT/opt/mfarm/mfarm-agent.py"
cp "$MFARM/miner-wrapper.sh" "$MNT/opt/mfarm/miner-wrapper.sh"
cp "$MFARM/mfarm-agent.service" "$MNT/etc/systemd/system/mfarm-agent.service"
chmod +x "$MNT/opt/mfarm/mfarm-agent.py" "$MNT/opt/mfarm/miner-wrapper.sh"

# Phone-home service (auto-discovery by MeowFarm)
cp /mnt/c/Source/mfarm/mfarm/worker/meowos-phonehome.py "$MNT/opt/mfarm/meowos-phonehome.py"
cp /mnt/c/Source/mfarm/mfarm/worker/meowos-phonehome.service "$MNT/etc/systemd/system/meowos-phonehome.service"
chmod +x "$MNT/opt/mfarm/meowos-phonehome.py"

# CCminer v21.1.1
cd /tmp && tar xzf /mnt/c/Source/ccminer-patch/hiveos/ccminer-6390-v21.1.1.tar.gz 2>/dev/null || true
cp /tmp/ccminer "$MNT/opt/mfarm/miners/ccminer"
chmod +x "$MNT/opt/mfarm/miners/ccminer"

# CUDA 12 runtime
cp /mnt/c/Users/benef/libcudart12.deb "$MNT/tmp/libcudart12.deb"
chroot "$MNT" bash -c 'dpkg -i --force-depends /tmp/libcudart12.deb 2>/dev/null; rm /tmp/libcudart12.deb'
echo '/usr/local/cuda-12.8/targets/x86_64-linux/lib' > "$MNT/etc/ld.so.conf.d/cuda-12.conf"
chroot "$MNT" ldconfig

# Lucky Pepe solo config
cat > "$MNT/etc/mfarm/config.json" <<'CFG'
{
    "agent": {"version":"0.1.0","stats_interval":5,"watchdog_interval":30,"max_gpu_temp":90,"critical_gpu_temp":95,"max_restarts_per_window":5,"restart_window_secs":600},
    "flight_sheet": {"name":"luckypepe-solo","coin":"LKPEPE","algo":"yescryptR32","miner":"ccminer","miner_version":"v21.1.1","pool_url":"http://192.168.68.78:9778","wallet":"luckypepe","worker":"%HOSTNAME%","password":"luckypepe123","extra_args":"--no-longpoll --timeout=30 --segwit","is_solo":true,"solo_rpc_user":"luckypepe","solo_rpc_pass":"luckypepe123","coinbase_addr":"LLhcyVdMJj7xLrTLRmhui1E4MB8AgHNB5Y"},
    "oc_profile": null,
    "miner_paths": {"ccminer":"/opt/mfarm/miners/ccminer"},
    "api_ports": {"ccminer":4068}
}
CFG

# OC script
cat > "$MNT/opt/mfarm/apply-oc.sh" <<'OC'
#!/bin/bash
sleep 5
killall Xorg 2>/dev/null
nohup Xorg :0 -config /etc/X11/xorg.conf > /dev/null 2>&1 &
sleep 4
export DISPLAY=:0
GPU_COUNT=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l)
for i in $(seq 0 $((GPU_COUNT-1))); do
  nvidia-settings -a "[gpu:$i]/GPUGraphicsClockOffsetAllPerformanceLevels=100" > /dev/null 2>&1
  nvidia-settings -a "[gpu:$i]/GPUMemoryTransferRateOffsetAllPerformanceLevels=2000" > /dev/null 2>&1
  nvidia-smi -i $i -lgc 2600,2600 > /dev/null 2>&1
done
nvidia-smi -pm 1 > /dev/null 2>&1
echo "OC applied at $(date)" >> /var/log/mfarm/oc.log
OC
chmod +x "$MNT/opt/mfarm/apply-oc.sh"

cat > "$MNT/etc/systemd/system/mfarm-oc.service" <<'SVC'
[Unit]
Description=MeowFarm GPU Overclock
After=multi-user.target
Wants=mfarm-agent.service

[Service]
Type=oneshot
ExecStart=/opt/mfarm/apply-oc.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVC

# Logrotate
cat > "$MNT/etc/logrotate.d/mfarm" <<'LR'
/var/log/mfarm/*.log { daily rotate 7 compress delaycompress missingok notifempty size 50M }
LR

# First-boot script
cat > "$MNT/opt/mfarm/mfarm-firstboot.sh" <<'FB'
#!/bin/bash
set -uo pipefail
mkdir -p /var/run/mfarm /var/log/mfarm
LOG="/var/log/mfarm/firstboot.log"
MARKER="/opt/mfarm/.firstboot-done"
exec > >(tee -a "$LOG") 2>&1
echo "=== MeowOS First-Boot ==="
if [[ -f "$MARKER" ]]; then echo "Already done."; systemctl disable mfarm-firstboot.service; exit 0; fi
for i in $(seq 1 30); do ping -c 1 -W 2 8.8.8.8 &>/dev/null && break; sleep 2; done
apt-get update -qq
if lspci | grep -qi nvidia; then
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:graphics-drivers/ppa
    apt-get update -qq
    apt-get install -y -qq ubuntu-drivers-common
    RECOMMENDED=$(ubuntu-drivers devices 2>/dev/null | grep "recommended" | head -1 | awk '{print $3}')
    [[ -n "$RECOMMENDED" ]] && apt-get install -y -qq "$RECOMMENDED" || apt-get install -y -qq nvidia-driver-535
    nvidia-smi -pm 1 2>/dev/null || true
fi
sensors-detect --auto >/dev/null 2>&1 || true
MAC_SUFFIX=$(ip link show | grep -m1 "link/ether" | awk '{print $2}' | tr -d ':' | tail -c 5)
hostnamectl set-hostname "mfarm-rig-${MAC_SUFFIX}"
sed -i "s/mfarm-rig/mfarm-rig-${MAC_SUFFIX}/g" /etc/hosts
# Skip nvidia-xconfig on CPU-only rigs (mini-series etc.) — calling it
# without GPUs prints a misleading "ERROR: Unable to determine number
# of GPUs" into the firstboot log.
if lspci | grep -qi nvidia; then
    nvidia-xconfig --enable-all-gpus --cool-bits=31 --allow-empty-initial-configuration 2>/dev/null || true
fi
mkdir -p /var/run/mfarm
python3 -c "
import json,subprocess,os,socket
hw={}
try:
    r=subprocess.run(['nvidia-smi','--query-gpu=name,pci.bus_id,memory.total','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=10)
    if r.returncode==0:
        gpus=[]
        for line in r.stdout.strip().split('\n'):
            if line.strip():
                parts=[p.strip() for p in line.split(',')]
                gpus.append({'name':parts[0],'pci_bus':parts[1],'vram_mb':int(parts[2])})
        hw['gpus']=gpus;hw['gpu_vendor']='nvidia'
except: pass
try:
    with open('/proc/cpuinfo') as f:
        for line in f:
            if 'model name' in line: hw['cpu_model']=line.split(':')[1].strip(); break
    hw['cpu_cores']=os.cpu_count()
except: pass
hw['hostname']=socket.gethostname()
with open('/var/run/mfarm/hwinfo.json','w') as f: json.dump(hw,f,indent=2)
print(json.dumps(hw,indent=2))
" || true
systemctl enable mfarm-agent
systemctl enable mfarm-oc.service
systemctl enable meowos-phonehome.service
systemctl start meowos-phonehome.service
touch "$MARKER"
systemctl disable mfarm-firstboot.service
echo "=== MeowOS First-Boot Complete ==="
echo "  Hostname: $(hostname)"
echo "  Rebooting in 10s..."
sleep 10
reboot
FB
chmod +x "$MNT/opt/mfarm/mfarm-firstboot.sh"

cat > "$MNT/etc/systemd/system/mfarm-firstboot.service" <<'FBSVC'
[Unit]
Description=MeowOS First-Boot Provisioning
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/opt/mfarm/.firstboot-done

[Service]
Type=oneshot
ExecStart=/opt/mfarm/mfarm-firstboot.sh
RemainAfterExit=yes
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
FBSVC

chroot "$MNT" systemctl enable mfarm-firstboot.service
echo "[3/5] Done"

echo "[4/5] Installing GRUB..."
cat > "$MNT/tmp/grub-setup.sh" <<'GRUB'
#!/bin/bash
set -euo pipefail
grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable 2>&1 || echo "grub-install warning"
cat > /etc/default/grub <<'GC'
GRUB_DEFAULT=0
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR="MeowOS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet"
GRUB_CMDLINE_LINUX="net.ifnames=0 biosdevname=0"
GRUB_TERMINAL="console"
GC
update-grub
echo "GRUB_DONE"
GRUB
chmod +x "$MNT/tmp/grub-setup.sh"
chroot "$MNT" /tmp/grub-setup.sh
echo "[4/5] Done"

echo "[5/5] Unmounting..."
umount "$MNT/dev/pts" 2>/dev/null || true
umount "$MNT/dev" 2>/dev/null || true
umount "$MNT/proc" 2>/dev/null || true
umount "$MNT/sys" 2>/dev/null || true
umount "$MNT/boot/efi" 2>/dev/null || true
umount "$MNT" 2>/dev/null || true

echo ""
echo "============================================"
echo "  MeowOS Install Complete!"
echo "============================================"
echo "  User:  miner / mfarm"
echo "  Root:  root / mfarm"
echo "  SSH key authorized"
echo "  CCminer v21.1.1 + Lucky Pepe solo"
echo "  OC: core +100, mem +2000, lock 2600"
echo "  First boot installs NVIDIA drivers"
echo "============================================"
