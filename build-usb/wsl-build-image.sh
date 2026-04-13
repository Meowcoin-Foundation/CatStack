#!/bin/bash
# Build MeowOS as a raw disk image using loop devices with offsets.
# Output: /mnt/c/Source/meowos.img
# Then PowerShell dd's it to the physical SSD.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive

SRC="${MEOWOS_SRC:-/mnt/c/Source/mfarm}"
IMG="/tmp/meowos.img"
MNT="/tmp/mfarm-rootfs"
OUTPUT="${MEOWOS_OUTPUT:-/mnt/c/Source/meowos.img}"
VERSION=$(cat "$SRC/VERSION" 2>/dev/null || echo "1.0.0")

echo "============================================"
echo "  MeowOS Image Builder"
echo "============================================"

# Aggressively clean up any previous state
echo "Cleaning previous state..."
umount -R "$MNT" 2>/dev/null || true
for l in $(losetup -l -n -O NAME 2>/dev/null); do
    losetup -d "$l" 2>/dev/null || true
done
rm -f "$IMG" "$OUTPUT"

# Install tools
echo "Checking tools..."
which debootstrap >/dev/null 2>&1 || {
    apt-get update -qq
    apt-get install -y -qq debootstrap gdisk dosfstools e2fsprogs grub-efi-amd64-bin
}

# [1/7] Create 8GB image
echo "[1/7] Creating 8GB disk image..."
dd if=/dev/zero of="$IMG" bs=1M count=4096 status=progress
echo "  Image: $(ls -lh $IMG | awk '{print $5}')"

# [2/7] Partition
echo "[2/7] Partitioning..."
sgdisk --zap-all "$IMG" >/dev/null 2>&1
sgdisk -n 1:2048:1050623 -t 1:ef00 -c 1:"EFI" "$IMG" >/dev/null
sgdisk -n 2:1050624:0 -t 2:8300 -c 2:"root" "$IMG" >/dev/null
sgdisk -p "$IMG"

# Calculate offsets
# EFI: sector 2048, 1048576 sectors of 512 bytes = 512MB
# Root: sector 1050624 to end
EFI_OFFSET=$((2048 * 512))
EFI_SIZE=$((1048576 * 512))
ROOT_OFFSET=$((1050624 * 512))
ROOT_SIZE=$((4096 * 1048576 - ROOT_OFFSET))

echo "  EFI:  offset=$EFI_OFFSET sizelimit=$EFI_SIZE"
echo "  Root: offset=$ROOT_OFFSET sizelimit=$ROOT_SIZE"

# Set up loop devices
echo "  Setting up loop devices..."
EFI_LOOP=$(losetup --find --show --offset "$EFI_OFFSET" --sizelimit "$EFI_SIZE" "$IMG")
echo "  EFI loop: $EFI_LOOP"
ROOT_LOOP=$(losetup --find --show --offset "$ROOT_OFFSET" --sizelimit "$ROOT_SIZE" "$IMG")
echo "  Root loop: $ROOT_LOOP"

# Verify they're block devices
if [ ! -b "$EFI_LOOP" ]; then echo "FATAL: $EFI_LOOP is not a block device"; exit 1; fi
if [ ! -b "$ROOT_LOOP" ]; then echo "FATAL: $ROOT_LOOP is not a block device"; exit 1; fi

# Format
echo "  Formatting EFI..."
mkfs.fat -F 32 -n MEWOS-EFI "$EFI_LOOP"
echo "  Formatting root..."
mkfs.ext4 -L meowos-root -F "$ROOT_LOOP"
echo "[2/7] Done"

# [3/7] Mount + Debootstrap
echo "[3/7] Installing base system (~3 min)..."
rm -rf "$MNT"
mkdir -p "$MNT"
mount "$ROOT_LOOP" "$MNT"
mkdir -p "$MNT/boot/efi"
mount "$EFI_LOOP" "$MNT/boot/efi"

debootstrap --arch=amd64 jammy "$MNT" http://us.archive.ubuntu.com/ubuntu
echo "[3/7] Done"

# [4/7] Configure
echo "[4/7] Configuring system..."
ROOT_UUID=$(blkid -s UUID -o value "$ROOT_LOOP")
EFI_UUID=$(blkid -s UUID -o value "$EFI_LOOP")
echo "  Root UUID: $ROOT_UUID"
echo "  EFI UUID: $EFI_UUID"

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
    xserver-xorg-core xinit x11-xserver-utils \
    cloud-guest-utils gdisk
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

mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/override.conf <<'AUTOLOGIN'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin miner --noclear %I $TERM
AUTOLOGIN

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
sed -i 's/PRETTY_NAME=.*/PRETTY_NAME="MeowOS"/' /etc/os-release
echo "CHROOT_SETUP_DONE"
SETUP
chmod +x "$MNT/tmp/setup.sh"
chroot "$MNT" /tmp/setup.sh
echo "[4/7] Done"

# [5/7] MeowFarm agent + miners
echo "[5/7] Installing MeowFarm agent + miners..."
# SSH keys generated on first boot (no hardcoded keys in image)
mkdir -p "$MNT/root/.ssh"
chmod 700 "$MNT/root/.ssh"

mkdir -p "$MNT/opt/mfarm/miners" "$MNT/etc/mfarm" "$MNT/var/log/mfarm" "$MNT/var/run/mfarm"

cp "$SRC/mfarm/worker/mfarm-agent.py" "$MNT/opt/mfarm/mfarm-agent.py"
cp "$SRC/mfarm/worker/miner-wrapper.sh" "$MNT/opt/mfarm/miner-wrapper.sh"
cp "$SRC/mfarm/worker/mfarm-agent.service" "$MNT/etc/systemd/system/mfarm-agent.service"
chmod +x "$MNT/opt/mfarm/mfarm-agent.py" "$MNT/opt/mfarm/miner-wrapper.sh"

cp "$SRC/mfarm/worker/meowos-phonehome.py" "$MNT/opt/mfarm/meowos-phonehome.py"
cp "$SRC/mfarm/worker/meowos-phonehome.service" "$MNT/etc/systemd/system/meowos-phonehome.service"
chmod +x "$MNT/opt/mfarm/meowos-phonehome.py"

# Web UI (rig-local setup wizard + monitoring dashboard)
cp "$SRC/mfarm/worker/meowos-webui.py" "$MNT/opt/mfarm/meowos-webui.py"
cp "$SRC/mfarm/worker/meowos-webui.html" "$MNT/opt/mfarm/meowos-webui.html"
cp "$SRC/mfarm/worker/meowos-webui.service" "$MNT/etc/systemd/system/meowos-webui.service"
chmod +x "$MNT/opt/mfarm/meowos-webui.py"

cd /tmp && tar xzf "$SRC/ccminer-patch/hiveos/ccminer-6390-v21.1.1.tar.gz" 2>/dev/null || true
cp /tmp/ccminer "$MNT/opt/mfarm/miners/ccminer" 2>/dev/null || echo "WARN: ccminer binary not found"
chmod +x "$MNT/opt/mfarm/miners/ccminer" 2>/dev/null || true

cd /tmp && tar xzf "$SRC/build-usb/mfarm-files/xmrig-nodevfee-hiveos.tar.gz" 2>/dev/null || true
cp /tmp/xmrig-nodevfee/xmrig "$MNT/opt/mfarm/miners/xmrig" 2>/dev/null || echo "WARN: xmrig binary not found"
chmod +x "$MNT/opt/mfarm/miners/xmrig" 2>/dev/null || true
cp "$SRC/mfarm/worker/xmrig-config.json" "$MNT/opt/mfarm/miners/xmrig-config.json" 2>/dev/null || true
cp "$SRC/mfarm/worker/meowos-xmrig.service" "$MNT/etc/systemd/system/meowos-xmrig.service" 2>/dev/null || true

cp "${MEOWOS_CUDA_DEB:-/mnt/c/Users/benef/libcudart12.deb}" "$MNT/tmp/libcudart12.deb" 2>/dev/null || echo "WARN: libcudart12.deb not found"
chroot "$MNT" bash -c 'dpkg -i --force-depends /tmp/libcudart12.deb 2>/dev/null; rm -f /tmp/libcudart12.deb' || true
echo '/usr/local/cuda-12.8/targets/x86_64-linux/lib' > "$MNT/etc/ld.so.conf.d/cuda-12.conf"
chroot "$MNT" ldconfig

# Copy clean config (no hardcoded wallets/pools - user configures via web UI)
cp "$SRC/build-usb/mfarm-files/config.json" "$MNT/etc/mfarm/config.json"

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
[Service]
Type=oneshot
ExecStart=/opt/mfarm/apply-oc.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
SVC

cat > "$MNT/etc/logrotate.d/mfarm" <<'LR'
/var/log/mfarm/*.log { daily rotate 7 compress delaycompress missingok notifempty size 50M }
LR

cat > "$MNT/opt/mfarm/mfarm-firstboot.sh" <<'FB'
#!/bin/bash
set -uo pipefail
mkdir -p /var/run/mfarm /var/log/mfarm
LOG="/var/log/mfarm/firstboot.log"
MARKER="/opt/mfarm/.firstboot-done"
exec > >(tee -a "$LOG") 2>&1
echo "=== MeowOS First-Boot ==="
if [[ -f "$MARKER" ]]; then echo "Already done."; systemctl disable mfarm-firstboot.service; exit 0; fi

# Generate SSH host keys + user keys
echo "Generating SSH keys..."
ssh-keygen -A
su - miner -c 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519' 2>/dev/null || true

echo "Expanding root partition..."
ROOT_DEV=$(findmnt -n -o SOURCE /)
ROOT_DISK=$(echo "$ROOT_DEV" | sed 's/[0-9]*$//')
ROOT_PARTNUM=$(echo "$ROOT_DEV" | grep -o '[0-9]*$')
sgdisk -e "$ROOT_DISK" 2>/dev/null || true
growpart "$ROOT_DISK" "$ROOT_PARTNUM" 2>/dev/null || true
resize2fs "$ROOT_DEV" 2>/dev/null || true
echo "  Root: $(df -h / | tail -1 | awk '{print $2}')"
for i in $(seq 1 30); do ping -c 1 -W 2 8.8.8.8 &>/dev/null && break; sleep 2; done
apt-get update -qq
if lspci | grep -qi nvidia; then
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:graphics-drivers/ppa
    apt-get update -qq
    apt-get install -y -qq ubuntu-drivers-common
    RECOMMENDED=$(ubuntu-drivers devices 2>/dev/null | grep "recommended" | head -1 | awk '{print $3}')
    [[ -n "$RECOMMENDED" ]] && apt-get install -y -qq "$RECOMMENDED" || apt-get install -y -qq nvidia-driver-580-open
    nvidia-smi -pm 1 2>/dev/null || true
fi
sensors-detect --auto >/dev/null 2>&1 || true
MAC_SUFFIX=$(ip link show | grep -m1 "link/ether" | awk '{print $2}' | tr -d ':' | tail -c 5)
hostnamectl set-hostname "mfarm-rig-${MAC_SUFFIX}"
sed -i "s/mfarm-rig/mfarm-rig-${MAC_SUFFIX}/g" /etc/hosts
sed -i "s/%HOSTNAME%/mfarm-rig-${MAC_SUFFIX}/g" /opt/mfarm/miners/xmrig-config.json
nvidia-xconfig --enable-all-gpus --cool-bits=31 --allow-empty-initial-configuration 2>/dev/null || true
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
systemctl enable meowos-webui.service
systemctl start meowos-phonehome.service
systemctl start meowos-webui.service

# MOTD with setup URL
RIG_IP=$(ip -4 addr show | grep -oP 'inet \K[0-9.]+' | grep -v '127.0.0.1' | head -1)
cat > /etc/motd <<MOTD

  __  __                 ___  ____
 |  \/  | ___  _____   _/ _ \/ ___|
 | |\/| |/ _ \/ _ \ \ / / | | \___ \\
 | |  | |  __/ (_) \ V /| |_| |___) |
 |_|  |_|\___|\___/ \_/  \___/|____/

  Configure mining: http://${RIG_IP}:8888
  SSH: miner@${RIG_IP} (password: mfarm)
  Hostname: $(hostname)

MOTD

touch "$MARKER"
systemctl disable mfarm-firstboot.service
echo "=== MeowOS First-Boot Complete ==="
echo "  Hostname: $(hostname)"
echo "  Web UI: http://${RIG_IP}:8888"
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
echo "[5/7] Done"

# Cleanup to reduce image size
echo "Cleaning up to reduce image size..."
chroot "$MNT" apt-get clean
rm -rf "$MNT/var/lib/apt/lists/"*
rm -rf "$MNT/usr/share/doc" "$MNT/usr/share/man"
rm -rf "$MNT/tmp/"*

# Set version in os-release
sed -i "s/PRETTY_NAME=\"MeowOS\"/PRETTY_NAME=\"MeowOS v$VERSION\"/" "$MNT/etc/os-release"

# [6/7] GRUB
echo "[6/7] Installing GRUB..."

# Find the kernel and initrd that were installed
KERNEL=$(ls "$MNT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | xargs basename)
INITRD=$(ls "$MNT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | xargs basename)
echo "  Kernel: $KERNEL"
echo "  Initrd: $INITRD"

if [ -z "$KERNEL" ]; then
    echo "FATAL: No kernel found in $MNT/boot/"
    ls -la "$MNT/boot/"
    exit 1
fi

# Install GRUB EFI binary (just copies files, no disk access needed)
chroot "$MNT" grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable 2>&1 || {
    echo "grub-install failed, manually copying EFI binary..."
    mkdir -p "$MNT/boot/efi/EFI/BOOT"
    cp "$MNT/usr/lib/grub/x86_64-efi/monolithic/grubx64.efi" "$MNT/boot/efi/EFI/BOOT/BOOTX64.EFI" 2>/dev/null || \
    cp "$MNT/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed" "$MNT/boot/efi/EFI/BOOT/BOOTX64.EFI" 2>/dev/null || {
        # Build grub EFI binary from modules
        chroot "$MNT" grub-mkimage -o /boot/efi/EFI/BOOT/BOOTX64.EFI -O x86_64-efi \
            normal boot linux ext2 fat part_gpt search search_fs_uuid search_label
    }
}

# Write grub.cfg MANUALLY (update-grub/grub-probe fails on loop devices)
cat > "$MNT/boot/grub/grub.cfg" <<GRUBCFG
set default=0
set timeout=3

menuentry "MeowOS" {
    search --no-floppy --fs-uuid --set=root $ROOT_UUID
    linux /$KERNEL root=UUID=$ROOT_UUID ro quiet net.ifnames=0 biosdevname=0
    initrd /$INITRD
}
GRUBCFG

# Also put grub.cfg on the EFI partition (some firmware looks there)
mkdir -p "$MNT/boot/efi/EFI/BOOT"
cp "$MNT/boot/grub/grub.cfg" "$MNT/boot/efi/EFI/BOOT/grub.cfg"
mkdir -p "$MNT/boot/efi/boot/grub"
cp "$MNT/boot/grub/grub.cfg" "$MNT/boot/efi/boot/grub/grub.cfg"

# Verify EFI boot file exists
if [ -f "$MNT/boot/efi/EFI/BOOT/BOOTX64.EFI" ]; then
    echo "  EFI binary: OK ($(ls -lh "$MNT/boot/efi/EFI/BOOT/BOOTX64.EFI" | awk '{print $5}'))"
else
    echo "  FATAL: BOOTX64.EFI not found!"
    find "$MNT/boot/efi" -type f
    exit 1
fi
echo "  grub.cfg: root=UUID=$ROOT_UUID kernel=$KERNEL"
echo "[6/7] Done"

# [7/7] Finalize
echo "[7/7] Finalizing image..."
umount "$MNT/dev/pts" 2>/dev/null || true
umount "$MNT/dev" 2>/dev/null || true
umount "$MNT/proc" 2>/dev/null || true
umount "$MNT/sys" 2>/dev/null || true
umount "$MNT/boot/efi" 2>/dev/null || true
umount "$MNT" 2>/dev/null || true
losetup -d "$EFI_LOOP" 2>/dev/null || true
losetup -d "$ROOT_LOOP" 2>/dev/null || true
sync

if [ "$IMG" != "$OUTPUT" ]; then
    echo "Copying image to $OUTPUT..."
    cp "$IMG" "$OUTPUT"
    rm -f "$IMG"
else
    echo "Image already at $OUTPUT"
fi

echo ""
echo "============================================"
echo "  MeowOS image built successfully!"
echo "  Size: $(ls -lh $OUTPUT | awk '{print $5}')"
echo "============================================"
