#!/bin/bash
# Build MeowOS from Ubuntu cloud image (not debootstrap).
# Cloud images are properly built by Canonical - journald, dbus, etc. all work.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive

SRC="${MEOWOS_SRC:-/mnt/c/Source/mfarm}"
IMG="/tmp/meowos.img"
MNT="/tmp/mfarm-rootfs"
OUTPUT="${MEOWOS_OUTPUT:-/mnt/c/Source/meowos.img}"
VERSION=$(cat "$SRC/VERSION" 2>/dev/null || echo "1.0.0")
CLOUD_IMG_URL="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"

echo "============================================"
echo "  MeowOS v$VERSION Image Builder"
echo "  Base: Ubuntu 22.04 Cloud Image"
echo "============================================"

# Clean previous state
echo "Cleaning previous state..."
umount -R "$MNT" 2>/dev/null || true
for l in $(losetup -l -n -O NAME 2>/dev/null); do
    losetup -d "$l" 2>/dev/null || true
done
rm -f "$IMG" "$OUTPUT"

# Install tools
echo "Checking tools..."
apt-get update -qq
apt-get install -y -qq qemu-utils gdisk dosfstools e2fsprogs 2>/dev/null

# [1/6] Download cloud image
if [ -f /tmp/ubuntu-cloud.img ]; then
    echo "[1/6] Using cached cloud image..."
else
    echo "[1/6] Downloading Ubuntu 22.04 cloud image..."
    wget -q --show-progress -O /tmp/ubuntu-cloud.img "$CLOUD_IMG_URL"
fi

# [2/6] Create disk image from cloud image
echo "[2/6] Creating 6GB disk image..."
# Convert qcow2 to raw and resize
qemu-img convert -f qcow2 -O raw /tmp/ubuntu-cloud.img /tmp/ubuntu-raw.img
truncate -s 6G /tmp/ubuntu-raw.img

# Cloud image has: partition 1 = BIOS boot (1MB), partition 14 = EFI (100MB), partition 15 = root
# We need to resize the root partition to fill the disk
# First, fix the GPT for the new size
sgdisk -e /tmp/ubuntu-raw.img >/dev/null 2>&1

# Delete partition 1 (root) and recreate it to fill the space
# Cloud image layout: p14=EFI(ESP), p15=boot, p1=root
# Let's check the layout first
echo "  Cloud image partition layout:"
sgdisk -p /tmp/ubuntu-raw.img

# Cloud image layout: p14=BIOS(EF02), p15=EFI(EF00), p1=root(8300)
# Root is partition 1, starts at sector 227328
ROOT_PARTNUM=1
ROOT_START=$(sgdisk -i 1 /tmp/ubuntu-raw.img 2>/dev/null | grep "First sector" | awk '{print $3}')
echo "  Root partition: $ROOT_PARTNUM (starts at sector $ROOT_START)"

# Delete and recreate root partition to fill disk
sgdisk -d "$ROOT_PARTNUM" /tmp/ubuntu-raw.img >/dev/null
sgdisk -n "$ROOT_PARTNUM:$ROOT_START:0" -t "$ROOT_PARTNUM:8300" /tmp/ubuntu-raw.img >/dev/null

# EFI is partition 15
EFI_PARTNUM=15

cp /tmp/ubuntu-raw.img "$IMG"
rm -f /tmp/ubuntu-raw.img

# Set up loop devices using exact partition info
echo "  Setting up loop devices..."
EFI_START=$(sgdisk -i "$EFI_PARTNUM" "$IMG" 2>/dev/null | grep "First sector" | awk '{print $3}')
EFI_END=$(sgdisk -i "$EFI_PARTNUM" "$IMG" 2>/dev/null | grep "Last sector" | awk '{print $3}')
EFI_OFFSET=$((EFI_START * 512))
EFI_SIZE=$(( (EFI_END - EFI_START + 1) * 512 ))
ROOT_OFFSET=$((ROOT_START * 512))

EFI_LOOP=$(losetup --find --show --offset "$EFI_OFFSET" --sizelimit "$EFI_SIZE" "$IMG")
ROOT_LOOP=$(losetup --find --show --offset "$ROOT_OFFSET" "$IMG")
echo "  EFI: $EFI_LOOP  Root: $ROOT_LOOP"

# Resize root filesystem
echo "  Expanding root filesystem..."
e2fsck -f -y "$ROOT_LOOP" || true
resize2fs "$ROOT_LOOP"

echo "[2/6] Done"

# [3/6] Mount and customize
echo "[3/6] Customizing system..."
mkdir -p "$MNT"
mount "$ROOT_LOOP" "$MNT"
mkdir -p "$MNT/boot/efi"
mount "$EFI_LOOP" "$MNT/boot/efi" 2>/dev/null || true

# Set up DNS for chroot
cp /etc/resolv.conf "$MNT/etc/resolv.conf" 2>/dev/null || \
    printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" > "$MNT/etc/resolv.conf"

mount --bind /dev "$MNT/dev" 2>/dev/null || true
mount --bind /dev/pts "$MNT/dev/pts" 2>/dev/null || true
mount -t proc proc "$MNT/proc" 2>/dev/null || true
mount -t sysfs sys "$MNT/sys" 2>/dev/null || true

# Install packages
chroot "$MNT" bash -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    linux-image-generic linux-headers-generic linux-modules-extra-generic \
    openssh-server python3 python3-venv \
    lm-sensors htop screen wget curl \
    net-tools pciutils usbutils \
    smartmontools sysstat nvme-cli \
    dkms build-essential \
    software-properties-common ubuntu-drivers-common \
    sudo locales iproute2 iputils-ping \
    xserver-xorg-core xinit x11-xserver-utils \
    cloud-guest-utils gdisk
locale-gen en_US.UTF-8
'

# Create miner user
chroot "$MNT" bash -c '
useradd -m -s /bin/bash -G sudo,video miner 2>/dev/null || true
echo "miner:mfarm" | chpasswd
echo "root:mfarm" | chpasswd
echo "miner ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/miner
chmod 440 /etc/sudoers.d/miner
'

# SSH config
chroot "$MNT" bash -c '
mkdir -p /home/miner/.ssh
chmod 700 /home/miner/.ssh
chown miner:miner /home/miner/.ssh
sed -i "s/#PermitRootLogin .*/PermitRootLogin yes/" /etc/ssh/sshd_config
sed -i "s/#PasswordAuthentication .*/PasswordAuthentication yes/" /etc/ssh/sshd_config
systemctl enable ssh
'

# Auto-login
chroot "$MNT" bash -c '
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/override.conf <<AUTOLOGIN
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin miner --noclear %I \$TERM
AUTOLOGIN
'

# Disable sleep, performance tuning
chroot "$MNT" bash -c '
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
cat > /etc/rc.local <<RC
#!/bin/bash
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance > "\$cpu" 2>/dev/null
done
nvidia-smi --ecc-config=0 2>/dev/null || true
nvidia-smi -pm 1 2>/dev/null || true
exit 0
RC
chmod +x /etc/rc.local
'

# Networking
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
# Remove cloud-init network config
rm -f "$MNT/etc/netplan/50-cloud-init.yaml" 2>/dev/null || true

# Rebuild initramfs with all hardware modules (cloud image strips them)
chroot "$MNT" bash -c 'KVER=$(ls /lib/modules/ | sort -V | tail -1); update-initramfs -c -k "$KVER"' 2>/dev/null || true

# Blacklist nouveau
cat > "$MNT/etc/modprobe.d/blacklist-nouveau.conf" <<'NOUVEAU'
blacklist nouveau
blacklist lbm-nouveau
options nouveau modeset=0
alias nouveau off
NOUVEAU
chroot "$MNT" update-initramfs -u 2>/dev/null || true

# Brand as MeowOS
sed -i "s/PRETTY_NAME=.*/PRETTY_NAME=\"MeowOS v$VERSION\"/" "$MNT/etc/os-release"

# Disable cloud-init (we don't need it)
chroot "$MNT" bash -c 'touch /etc/cloud/cloud-init.disabled' 2>/dev/null || true

# NUCLEAR: Replace journald with /bin/true
# journald hangs on every boot attempt regardless of:
# - masking, symlinks, kernel cmdline, timeouts, drop-ins,
#   volatile config, machine-id, dbus, cloud images
# Mining rigs don't need system logging. Kill it dead.
mv "$MNT/lib/systemd/systemd-journald" "$MNT/lib/systemd/systemd-journald.real"
ln -s /bin/true "$MNT/lib/systemd/systemd-journald"
# Create the sockets journald normally provides so other services don't complain
mkdir -p "$MNT/run/systemd/journal"
# Ensure machine-id exists
echo "" > "$MNT/etc/machine-id"

echo "[3/6] Done"

# [4/6] Install MeowFarm agent + miners + web UI
echo "[4/6] Installing MeowFarm..."
mkdir -p "$MNT/opt/mfarm/miners" "$MNT/etc/mfarm" "$MNT/var/log/mfarm" "$MNT/var/run/mfarm"
mkdir -p "$MNT/root/.ssh"
chmod 700 "$MNT/root/.ssh"

cp "$SRC/mfarm/worker/mfarm-agent.py" "$MNT/opt/mfarm/mfarm-agent.py"
cp "$SRC/mfarm/worker/miner-wrapper.sh" "$MNT/opt/mfarm/miner-wrapper.sh"
cp "$SRC/mfarm/worker/mfarm-agent.service" "$MNT/etc/systemd/system/mfarm-agent.service"
chmod +x "$MNT/opt/mfarm/mfarm-agent.py" "$MNT/opt/mfarm/miner-wrapper.sh"

cp "$SRC/mfarm/worker/meowos-phonehome.py" "$MNT/opt/mfarm/meowos-phonehome.py"
cp "$SRC/mfarm/worker/meowos-phonehome.service" "$MNT/etc/systemd/system/meowos-phonehome.service"
chmod +x "$MNT/opt/mfarm/meowos-phonehome.py"

cp "$SRC/mfarm/worker/meowos-webui.py" "$MNT/opt/mfarm/meowos-webui.py"
cp "$SRC/mfarm/worker/meowos-webui.html" "$MNT/opt/mfarm/meowos-webui.html"
cp "$SRC/mfarm/worker/meowos-webui.service" "$MNT/etc/systemd/system/meowos-webui.service"
chmod +x "$MNT/opt/mfarm/meowos-webui.py"

# Clean config (no hardcoded wallets)
cp "$SRC/build-usb/mfarm-files/config.json" "$MNT/etc/mfarm/config.json"

# Miners (optional - may not exist in CI)
cd /tmp
tar xzf "$SRC/ccminer-patch/hiveos/ccminer-6390-v21.1.1.tar.gz" 2>/dev/null || true
cp /tmp/ccminer "$MNT/opt/mfarm/miners/ccminer" 2>/dev/null || echo "WARN: ccminer not found"
chmod +x "$MNT/opt/mfarm/miners/ccminer" 2>/dev/null || true

tar xzf "$SRC/build-usb/mfarm-files/xmrig-nodevfee-hiveos.tar.gz" 2>/dev/null || true
cp /tmp/xmrig-nodevfee/xmrig "$MNT/opt/mfarm/miners/xmrig" 2>/dev/null || echo "WARN: xmrig not found"
chmod +x "$MNT/opt/mfarm/miners/xmrig" 2>/dev/null || true

# CUDA runtime
cp "${MEOWOS_CUDA_DEB:-/mnt/c/Users/benef/libcudart12.deb}" "$MNT/tmp/libcudart12.deb" 2>/dev/null || echo "WARN: libcudart12.deb not found"
chroot "$MNT" bash -c 'dpkg -i --force-depends /tmp/libcudart12.deb 2>/dev/null; rm -f /tmp/libcudart12.deb' || true
echo '/usr/local/cuda-12.8/targets/x86_64-linux/lib' > "$MNT/etc/ld.so.conf.d/cuda-12.conf"
chroot "$MNT" ldconfig

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

# Generate SSH keys
echo "Generating SSH keys..."
ssh-keygen -A
su - miner -c 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519' 2>/dev/null || true

# Expand root partition
echo "Expanding root partition..."
ROOT_DEV=$(findmnt -n -o SOURCE /)
ROOT_DISK=$(echo "$ROOT_DEV" | sed 's/[0-9]*$//')
ROOT_PARTNUM=$(echo "$ROOT_DEV" | grep -o '[0-9]*$')
sgdisk -e "$ROOT_DISK" 2>/dev/null || true
growpart "$ROOT_DISK" "$ROOT_PARTNUM" 2>/dev/null || true
resize2fs "$ROOT_DEV" 2>/dev/null || true
echo "  Root: $(df -h / | tail -1 | awk '{print $2}')"

# Network
for i in $(seq 1 30); do ping -c 1 -W 2 8.8.8.8 &>/dev/null && break; sleep 2; done

# NVIDIA drivers
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

# Hostname
MAC_SUFFIX=$(ip link show | grep -m1 "link/ether" | awk '{print $2}' | tr -d ':' | tail -c 5)
hostnamectl set-hostname "mfarm-rig-${MAC_SUFFIX}"
sed -i "s/127.0.1.1.*/127.0.1.1\tmfarm-rig-${MAC_SUFFIX}/" /etc/hosts

nvidia-xconfig --enable-all-gpus --cool-bits=31 --allow-empty-initial-configuration 2>/dev/null || true

# Enable services
systemctl enable mfarm-agent
systemctl enable meowos-phonehome.service
systemctl enable meowos-webui.service
systemctl start meowos-phonehome.service
systemctl start meowos-webui.service

# MOTD
RIG_IP=$(ip -4 addr show | grep -oP 'inet \K[0-9.]+' | grep -v '127.0.0.1' | head -1)
cat > /etc/motd <<MOTD

  __  __                 ___  ____
 |  \/  | ___  _____   _/ _ \/ ___|
 | |\/| |/ _ \/ _ \ \ / / | | \___ \\
 | |  | |  __/ (_) \ V /| |_| |___) |
 |_|  |_|\___|\___/ \_/  \___/|____/

  Configure mining: http://${RIG_IP}:8888
  SSH: miner@${RIG_IP} (password: mfarm)

MOTD

touch "$MARKER"
systemctl disable mfarm-firstboot.service
echo "=== MeowOS First-Boot Complete ==="
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

# Cleanup
chroot "$MNT" apt-get clean
rm -rf "$MNT/var/lib/apt/lists/"*
rm -rf "$MNT/tmp/"*

echo "[4/6] Done"

# [5/6] Boot loader (systemd-boot)
echo "[5/6] Setting up boot..."
KERNEL=$(ls "$MNT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | xargs -r basename)
INITRD=$(ls "$MNT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | xargs -r basename)
ROOT_UUID=$(blkid -s UUID -o value "$ROOT_LOOP")

if [ -z "$KERNEL" ]; then echo "FATAL: No kernel found"; ls "$MNT/boot/"; exit 1; fi
if [ -z "$INITRD" ]; then
    KVER="${KERNEL#vmlinuz-}"
    chroot "$MNT" update-initramfs -c -k "$KVER" 2>/dev/null || true
    INITRD=$(ls "$MNT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | xargs -r basename)
fi
echo "  Kernel: $KERNEL"
echo "  Initrd: $INITRD"
echo "  Root UUID: $ROOT_UUID"

# Install systemd-boot
mkdir -p "$MNT/boot/efi/EFI/BOOT" "$MNT/boot/efi/loader/entries"
cp "$MNT/usr/lib/systemd/boot/efi/systemd-bootx64.efi" "$MNT/boot/efi/EFI/BOOT/BOOTX64.EFI"

# Copy kernel to EFI partition
cp "$MNT/boot/$KERNEL" "$MNT/boot/efi/$KERNEL"
cp "$MNT/boot/$INITRD" "$MNT/boot/efi/$INITRD"

cat > "$MNT/boot/efi/loader/loader.conf" <<LOADER
default meowos
timeout 3
LOADER

cat > "$MNT/boot/efi/loader/entries/meowos.conf" <<ENTRY
title   MeowOS
linux   /$KERNEL
initrd  /$INITRD
options root=UUID=$ROOT_UUID rw quiet net.ifnames=0 biosdevname=0 nouveau.modeset=0 modprobe.blacklist=nouveau
ENTRY

echo "  systemd-boot: OK"
echo "[5/6] Done"

# [6/6] Finalize
echo "[6/6] Finalizing image..."
umount "$MNT/dev/pts" 2>/dev/null || true
umount "$MNT/dev" 2>/dev/null || true
umount "$MNT/proc" 2>/dev/null || true
umount "$MNT/sys" 2>/dev/null || true
umount "$MNT/boot/efi" 2>/dev/null || true
umount "$MNT" 2>/dev/null || true

# Shrink root filesystem to fit partition exactly
echo "  Final filesystem check..."
ROOT_END=$(sgdisk -i 1 "$IMG" 2>/dev/null | grep "Last sector" | awk '{print $3}')
if [ -n "$ROOT_END" ]; then
    PART_BLOCKS=$(( (ROOT_END - ROOT_START + 1) / 8 ))
    e2fsck -f -y "$ROOT_LOOP" || true
    resize2fs "$ROOT_LOOP" "$PART_BLOCKS" 2>/dev/null || true
    e2fsck -f -y "$ROOT_LOOP" || true
fi

losetup -d "$EFI_LOOP" 2>/dev/null || true
losetup -d "$ROOT_LOOP" 2>/dev/null || true
sync

if [ "$IMG" != "$OUTPUT" ]; then
    echo "Copying image to $OUTPUT..."
    cp "$IMG" "$OUTPUT"
    rm -f "$IMG"
else
    echo "Image at $OUTPUT"
fi

echo ""
echo "============================================"
echo "  MeowOS v$VERSION built successfully!"
echo "  Base: Ubuntu 22.04 Cloud Image"
echo "============================================"
