#!/bin/bash
#
# MFarm Direct NVMe Install
# Installs Ubuntu 22.04 + MFarm directly onto an NVMe drive from WSL.
# The drive is then ready to boot in a mining rig.
#
set -euo pipefail

DISK="/dev/sde"
MNT="/tmp/mfarm-rootfs"
MFARM_FILES="/mnt/c/Source/mfarm/build-usb/mfarm-files"
SSH_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIETCncNMggVWmhKhO8ylpK2g8/czRm6TeKOEDrga8MVr benefit14snake@hotmail.com"
USER_PASS='$6$6mvROTQewCK3GZWQ$1c4qBpc8ZH5vOnJkNu/fMGtsMB9rKFtEIdGqy6f4o.uTZiSGaW3KeTRoSePRGs7sdMEaVxU3GXbKVEh4Em3m2/'

echo "============================================"
echo "  MFarm NVMe Direct Install"
echo "  Target: $DISK ($(lsblk -nd -o SIZE $DISK))"
echo "============================================"
echo ""

# Safety check
if ! lsblk "$DISK" &>/dev/null; then
    echo "ERROR: $DISK not found"
    exit 1
fi

# ── 1. Install build tools ──────────────────────────────────────────

echo "[1/8] Installing build tools..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq debootstrap gdisk dosfstools e2fsprogs arch-install-scripts 2>/dev/null || {
    # arch-install-scripts provides genfstab; if not available we'll handle fstab manually
    apt-get install -y -qq debootstrap gdisk dosfstools e2fsprogs
}

# ── 2. Partition the drive ──────────────────────────────────────────

echo "[2/8] Partitioning $DISK..."
# Wipe existing partition table
sgdisk --zap-all "$DISK"

# Create partitions:
#   1: 512MB EFI System Partition (FAT32)
#   2: Rest as Linux root (ext4)
sgdisk -n 1:0:+512M -t 1:ef00 -c 1:"EFI" "$DISK"
sgdisk -n 2:0:0     -t 2:8300 -c 2:"root" "$DISK"
sgdisk -p "$DISK"

# Wait for kernel to re-read partition table
sleep 2
partprobe "$DISK" 2>/dev/null || true
sleep 2

# Verify partitions exist
if [[ ! -b "${DISK}1" ]]; then
    echo "  Waiting for partitions to appear..."
    sleep 3
fi

echo "  Partitions:"
lsblk "$DISK"

# ── 3. Format partitions ───────────────────────────────────────────

echo "[3/8] Formatting partitions..."
mkfs.fat -F 32 -n MFARM-EFI "${DISK}1"
mkfs.ext4 -L mfarm-root -F "${DISK}2"

# ── 4. Mount and debootstrap ───────────────────────────────────────

echo "[4/8] Installing Ubuntu 22.04 base system (this takes a few minutes)..."
mkdir -p "$MNT"
mount "${DISK}2" "$MNT"
mkdir -p "$MNT/boot/efi"
mount "${DISK}1" "$MNT/boot/efi"

debootstrap --arch=amd64 jammy "$MNT" http://archive.ubuntu.com/ubuntu

echo "  Base system installed"

# ── 5. Configure the system ────────────────────────────────────────

echo "[5/8] Configuring system..."

# Mount virtual filesystems for chroot
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"

# Generate fstab
ROOT_UUID=$(blkid -s UUID -o value "${DISK}2")
EFI_UUID=$(blkid -s UUID -o value "${DISK}1")

tee "$MNT/etc/fstab" > /dev/null << FSTAB
# MFarm Mining Rig
UUID=$ROOT_UUID   /          ext4   errors=remount-ro   0 1
UUID=$EFI_UUID    /boot/efi  vfat   umask=0077          0 1
FSTAB

# APT sources
tee "$MNT/etc/apt/sources.list" > /dev/null << 'SOURCES'
deb http://archive.ubuntu.com/ubuntu jammy main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu jammy-updates main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu jammy-security main restricted universe multiverse
SOURCES

# Hostname
echo "mfarm-rig" | tee "$MNT/etc/hostname" > /dev/null
tee "$MNT/etc/hosts" > /dev/null << 'HOSTS'
127.0.0.1   localhost
127.0.1.1   mfarm-rig
HOSTS

# Timezone
ln -sf /usr/share/zoneinfo/UTC "$MNT/etc/localtime"

# Networking - DHCP on all ethernet
mkdir -p "$MNT/etc/netplan"
tee "$MNT/etc/netplan/01-mfarm.yaml" > /dev/null << 'NETPLAN'
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
NETPLAN

# DNS resolver
tee "$MNT/etc/resolv.conf" > /dev/null << 'RESOLV'
nameserver 8.8.8.8
nameserver 1.1.1.1
RESOLV

# Now chroot and install packages + kernel
chroot "$MNT" /bin/bash << 'CHROOT_SCRIPT'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export HOME=/root

# Update package lists
apt-get update -qq

# Install kernel and essential packages
apt-get install -y -qq \
    linux-image-generic \
    linux-headers-generic \
    grub-efi-amd64 \
    systemd-sysv \
    openssh-server \
    python3 \
    python3-venv \
    lm-sensors \
    htop \
    screen \
    wget \
    curl \
    net-tools \
    pciutils \
    usbutils \
    smartmontools \
    sysstat \
    nvme-cli \
    dkms \
    build-essential \
    software-properties-common \
    ubuntu-drivers-common \
    \
    locales \
    iproute2 \
    iputils-ping \
    netplan.io \
    systemd-resolved

# Generate locale
locale-gen en_US.UTF-8

# ── Create miner user ──
useradd -m -s /bin/bash -G sudo,video,render miner
echo "miner:mfarm" | chpasswd
echo "root:mfarm" | chpasswd

# Sudo without password
echo 'miner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/miner
chmod 440 /etc/sudoers.d/miner

# ── SSH config ──
mkdir -p /home/miner/.ssh
chmod 700 /home/miner/.ssh
chown miner:miner /home/miner/.ssh

sed -i 's/#PermitRootLogin .*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/#PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config
systemctl enable ssh

# ── Disable unnecessary services ──
systemctl disable snapd.service 2>/dev/null || true
systemctl disable snapd.socket 2>/dev/null || true
systemctl disable multipathd.service 2>/dev/null || true
systemctl disable apparmor.service 2>/dev/null || true
systemctl disable ufw.service 2>/dev/null || true
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# ── Performance tuning ──
cat > /etc/rc.local << 'RCLOCAL'
#!/bin/bash
# Set CPU governor to performance
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance > "$cpu" 2>/dev/null
done
# Disable GPU ECC
nvidia-smi --ecc-config=0 2>/dev/null || true
# Enable persistence mode
nvidia-smi -pm 1 2>/dev/null || true
exit 0
RCLOCAL
chmod +x /etc/rc.local

# File descriptor limits
cat >> /etc/security/limits.conf << 'LIMITS'
*    soft    nofile    65535
*    hard    nofile    65535
LIMITS

# Kernel parameters for mining
cat > /etc/sysctl.d/99-mfarm.conf << 'SYSCTL'
# Mining rig optimizations
vm.swappiness=10
net.core.somaxconn=65535
kernel.panic=10
kernel.panic_on_oops=1
SYSCTL

# Enable systemd-networkd for netplan
systemctl enable systemd-networkd
systemctl enable systemd-resolved

CHROOT_SCRIPT

echo "  System configured"

# ── 6. Install SSH key ─────────────────────────────────────────────

echo "[6/8] Setting up SSH keys..."
echo "$SSH_PUBKEY" | tee "$MNT/home/miner/.ssh/authorized_keys" > /dev/null
chmod 600 "$MNT/home/miner/.ssh/authorized_keys"
chroot "$MNT" chown miner:miner /home/miner/.ssh/authorized_keys

# Also for root
mkdir -p "$MNT/root/.ssh"
echo "$SSH_PUBKEY" | tee "$MNT/root/.ssh/authorized_keys" > /dev/null
chmod 600 "$MNT/root/.ssh/authorized_keys"
chmod 700 "$MNT/root/.ssh"

# ── 7. Install MFarm agent + first-boot ────────────────────────────

echo "[7/8] Installing MFarm agent..."
mkdir -p "$MNT/opt/mfarm/miners" "$MNT/etc/mfarm" "$MNT/var/log/mfarm" "$MNT/var/run/mfarm"

cp "$MFARM_FILES/mfarm-agent.py"          "$MNT/opt/mfarm/mfarm-agent.py"
cp "$MFARM_FILES/miner-wrapper.sh"         "$MNT/opt/mfarm/miner-wrapper.sh"
cp "$MFARM_FILES/mfarm-agent.service"      "$MNT/etc/systemd/system/mfarm-agent.service"
cp "$MFARM_FILES/config.json"              "$MNT/etc/mfarm/config.json"
cp "$MFARM_FILES/mfarm-firstboot.sh"       "$MNT/opt/mfarm/mfarm-firstboot.sh"
cp "$MFARM_FILES/mfarm-firstboot.service"  "$MNT/etc/systemd/system/mfarm-firstboot.service"

chmod +x "$MNT/opt/mfarm/mfarm-agent.py"
chmod +x "$MNT/opt/mfarm/miner-wrapper.sh"
chmod +x "$MNT/opt/mfarm/mfarm-firstboot.sh"

# Log rotation
tee "$MNT/etc/logrotate.d/mfarm" > /dev/null << 'LR'
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

# Enable first-boot service (installs NVIDIA drivers on first real boot)
chroot "$MNT" systemctl enable mfarm-firstboot.service

echo "  MFarm agent installed"

# ── 8. Install GRUB bootloader ─────────────────────────────────────

echo "[8/8] Installing GRUB bootloader..."

# Bind mount EFI vars if available
if [[ -d /sys/firmware/efi ]]; then
    mount --bind /sys/firmware/efi/efivars "$MNT/sys/firmware/efi/efivars" 2>/dev/null || true
fi

chroot "$MNT" /bin/bash << 'GRUB_SCRIPT'
set -euo pipefail

# Install GRUB to the EFI partition
# Use --removable flag so it works on any UEFI motherboard without needing
# an NVRAM entry (the NVMe will be moved between machines)
grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=mfarm --removable 2>/dev/null || \
grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable

# Configure GRUB
cat > /etc/default/grub << 'GRUBCFG'
GRUB_DEFAULT=0
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR="MFarm"
GRUB_CMDLINE_LINUX_DEFAULT="quiet"
GRUB_CMDLINE_LINUX="net.ifnames=0 biosdevname=0"
GRUB_TERMINAL="console"
GRUBCFG

# Generate GRUB config
update-grub

GRUB_SCRIPT

echo "  GRUB installed"

# ── Cleanup and unmount ─────────────────────────────────────────────

echo ""
echo "Unmounting..."
umount "$MNT/sys/firmware/efi/efivars" 2>/dev/null || true
umount "$MNT/dev/pts"
umount "$MNT/dev"
umount "$MNT/proc"
umount "$MNT/sys"
umount "$MNT/boot/efi"
umount "$MNT"

echo ""
echo "============================================"
echo "  MFarm NVMe Install Complete!"
echo "============================================"
echo ""
echo "  Drive: $DISK ($(lsblk -nd -o SIZE $DISK))"
echo "  OS:    Ubuntu 22.04 LTS (Jammy)"
echo "  User:  miner / mfarm"
echo "  Root:  root / mfarm"
echo "  SSH:   Your ed25519 key is authorized"
echo ""
echo "  What happens when you put this in a rig:"
echo "    1. Boot from NVMe (UEFI)"
echo "    2. First-boot installs NVIDIA drivers (~5 min)"
echo "    3. Rig reboots, MFarm agent starts"
echo "    4. Add the rig from your PC:"
echo "         mfarm rig add <name> <ip>"
echo ""
echo "  To detach the NVMe from WSL, run:"
echo "    wsl --unmount \\\\.\PHYSICALDRIVE0"
echo "============================================"
