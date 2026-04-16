#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Find the NVMe - look for ~477G disk that isn't the WSL root
DISK=$(lsblk -nd -o NAME,SIZE,MOUNTPOINT | grep -v '/' | awk '$2 ~ /476/ {print "/dev/"$1}' | head -1)
if [ -z "$DISK" ]; then
    echo "ERROR: Cannot find NVMe disk"
    lsblk
    exit 1
fi
echo "Found NVMe at: $DISK"
MNT="/tmp/mfarm-rootfs"
MFARM="/mnt/c/Source/mfarm/build-usb/mfarm-files"

# Ensure clean mount state
umount "$MNT/dev/pts" 2>/dev/null || true
umount "$MNT/dev" 2>/dev/null || true
umount "$MNT/proc" 2>/dev/null || true
umount "$MNT/sys" 2>/dev/null || true
umount "$MNT/boot/efi" 2>/dev/null || true
umount "$MNT" 2>/dev/null || true
mkdir -p "$MNT"
mount "${DISK}2" "$MNT"
# Clean old contents
rm -rf "$MNT/lost+found" "$MNT"/* 2>/dev/null || true
mkdir -p "$MNT/boot/efi"
mount "${DISK}1" "$MNT/boot/efi"

echo "=== Debootstrap ==="
debootstrap --arch=amd64 jammy "$MNT" http://us.archive.ubuntu.com/ubuntu
echo "=== Base installed ==="

# Get UUIDs
ROOT_UUID=$(blkid -s UUID -o value "${DISK}2")
EFI_UUID=$(blkid -s UUID -o value "${DISK}1")

# fstab
cat > "$MNT/etc/fstab" <<EOF
UUID=$ROOT_UUID   /          ext4   errors=remount-ro   0 1
UUID=$EFI_UUID    /boot/efi  vfat   umask=0077          0 1
EOF

# APT
cat > "$MNT/etc/apt/sources.list" <<'EOF'
deb http://us.archive.ubuntu.com/ubuntu jammy main restricted universe multiverse
deb http://us.archive.ubuntu.com/ubuntu jammy-updates main restricted universe multiverse
deb http://us.archive.ubuntu.com/ubuntu jammy-security main restricted universe multiverse
EOF

# Hostname
echo "mfarm-rig" > "$MNT/etc/hostname"
printf "127.0.0.1\tlocalhost\n127.0.1.1\tmfarm-rig\n" > "$MNT/etc/hosts"

# Timezone
ln -sf /usr/share/zoneinfo/UTC "$MNT/etc/localtime"

# Netplan
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
EOF

# DNS
printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" > "$MNT/etc/resolv.conf"

# Mount for chroot
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"

echo "=== Chroot: installing kernel + packages ==="
# Write the chroot script to a file to avoid quoting hell
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
    sudo locales iproute2 iputils-ping netplan.io

locale-gen en_US.UTF-8

# User
useradd -m -s /bin/bash -G sudo,video miner
echo "miner:mfarm" | chpasswd
echo "root:mfarm" | chpasswd
echo 'miner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/miner
chmod 440 /etc/sudoers.d/miner

# SSH
mkdir -p /home/miner/.ssh
chmod 700 /home/miner/.ssh
chown miner:miner /home/miner/.ssh
sed -i 's/#PermitRootLogin .*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/#PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config
systemctl enable ssh

# Disable junk
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# Performance
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

# Brand as MeowOS
sed -i 's/PRETTY_NAME=.*/PRETTY_NAME="MeowOS 1.0"/' /etc/os-release

echo "CHROOT_SETUP_DONE"
SETUP

chmod +x "$MNT/tmp/setup.sh"
chroot "$MNT" /tmp/setup.sh

echo "=== SSH keys ==="
PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIETCncNMggVWmhKhO8ylpK2g8/czRm6TeKOEDrga8MVr benefit14snake@hotmail.com"
echo "$PUBKEY" > "$MNT/home/miner/.ssh/authorized_keys"
chmod 600 "$MNT/home/miner/.ssh/authorized_keys"
chroot "$MNT" chown miner:miner /home/miner/.ssh/authorized_keys
mkdir -p "$MNT/root/.ssh"
echo "$PUBKEY" > "$MNT/root/.ssh/authorized_keys"
chmod 600 "$MNT/root/.ssh/authorized_keys"
chmod 700 "$MNT/root/.ssh"

echo "=== MFarm agent ==="
mkdir -p "$MNT/opt/mfarm/miners" "$MNT/etc/mfarm" "$MNT/var/log/mfarm" "$MNT/var/run/mfarm"
cp "$MFARM/mfarm-agent.py"         "$MNT/opt/mfarm/mfarm-agent.py"
cp "$MFARM/miner-wrapper.sh"        "$MNT/opt/mfarm/miner-wrapper.sh"
cp "$MFARM/mfarm-agent.service"     "$MNT/etc/systemd/system/mfarm-agent.service"
cp "$MFARM/config.json"             "$MNT/etc/mfarm/config.json"
cp "$MFARM/mfarm-firstboot.sh"      "$MNT/opt/mfarm/mfarm-firstboot.sh"
cp "$MFARM/mfarm-firstboot.service" "$MNT/etc/systemd/system/mfarm-firstboot.service"
chmod +x "$MNT/opt/mfarm/mfarm-agent.py" "$MNT/opt/mfarm/miner-wrapper.sh" "$MNT/opt/mfarm/mfarm-firstboot.sh"

cat > "$MNT/etc/logrotate.d/mfarm" <<'LR'
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

chroot "$MNT" systemctl enable mfarm-firstboot.service

echo "=== GRUB ==="
cat > "$MNT/tmp/grub-setup.sh" <<'GRUBSCRIPT'
#!/bin/bash
set -euo pipefail
grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable 2>&1 || echo "grub-install warning (expected in WSL)"

cat > /etc/default/grub <<'GC'
GRUB_DEFAULT=0
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR="MFarm"
GRUB_CMDLINE_LINUX_DEFAULT="quiet"
GRUB_CMDLINE_LINUX="net.ifnames=0 biosdevname=0"
GRUB_TERMINAL="console"
GC

update-grub
echo "GRUB_DONE"
GRUBSCRIPT
chmod +x "$MNT/tmp/grub-setup.sh"
chroot "$MNT" /tmp/grub-setup.sh

echo "=== Unmounting ==="
umount "$MNT/dev/pts" 2>/dev/null || true
umount "$MNT/dev" 2>/dev/null || true
umount "$MNT/proc" 2>/dev/null || true
umount "$MNT/sys" 2>/dev/null || true
umount "$MNT/boot/efi" 2>/dev/null || true
umount "$MNT" 2>/dev/null || true

echo ""
echo "============================================"
echo "  MFarm NVMe Install Complete!"
echo "============================================"
echo "  User:  miner / mfarm"
echo "  Root:  root / mfarm"
echo "  SSH key authorized"
echo "  First boot installs NVIDIA drivers"
echo "============================================"
