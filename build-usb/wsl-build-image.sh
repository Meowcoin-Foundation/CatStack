#!/bin/bash
# MeowOS Image Builder - debootstrap + systemd-boot
# Builds a bootable raw disk image with loop device offsets
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive

SRC="${MEOWOS_SRC:-/mnt/c/Source/mfarm}"
IMG="/tmp/meowos.img"
MNT="/tmp/mfarm-rootfs"
OUTPUT="${MEOWOS_OUTPUT:-/mnt/c/Source/meowos.img}"
VERSION=$(cat "$SRC/VERSION" 2>/dev/null || echo "1.0.0")

echo "============================================"
echo "  MeowOS v$VERSION Image Builder"
echo "============================================"

# Clean previous state
umount -R "$MNT" 2>/dev/null || true
for l in $(losetup -l -n -O NAME 2>/dev/null); do losetup -d "$l" 2>/dev/null || true; done
rm -f "$IMG" "$OUTPUT"

# Install tools
which debootstrap >/dev/null 2>&1 || {
    apt-get update -qq
    apt-get install -y -qq debootstrap gdisk dosfstools e2fsprogs
}

# [1/7] Create image
echo "[1/7] Creating 6GB image..."
dd if=/dev/zero of="$IMG" bs=1M count=10240 status=progress

# [2/7] Partition
echo "[2/7] Partitioning..."
sgdisk --zap-all "$IMG" >/dev/null 2>&1
sgdisk -n 1:2048:1050623 -t 1:ef00 -c 1:"EFI" "$IMG" >/dev/null
sgdisk -n 2:1050624:0 -t 2:8300 -c 2:"root" "$IMG" >/dev/null

EFI_OFFSET=$((2048 * 512))
EFI_SIZE=$((1048576 * 512))
ROOT_OFFSET=$((1050624 * 512))

# Get exact root partition end from sgdisk
ROOT_END=$(sgdisk -i 2 "$IMG" 2>/dev/null | grep "Last sector" | awk '{print $3}')
ROOT_SIZE=$(( (ROOT_END - 1050624 + 1) * 512 ))
echo "  Root: sectors 1050624-$ROOT_END = $ROOT_SIZE bytes"

EFI_LOOP=$(losetup --find --show --offset "$EFI_OFFSET" --sizelimit "$EFI_SIZE" "$IMG")
ROOT_LOOP=$(losetup --find --show --offset "$ROOT_OFFSET" --sizelimit "$ROOT_SIZE" "$IMG")
echo "  EFI: $EFI_LOOP  Root: $ROOT_LOOP"

mkfs.fat -F 32 -n MEWOS-EFI "$EFI_LOOP"
mkfs.ext4 -L meowos-root -F "$ROOT_LOOP"
echo "[2/7] Done"

# [3/7] Debootstrap
echo "[3/7] Installing base system (~3 min)..."
rm -rf "$MNT"; mkdir -p "$MNT"
mount "$ROOT_LOOP" "$MNT"
mkdir -p "$MNT/boot/efi"
mount "$EFI_LOOP" "$MNT/boot/efi"

debootstrap --arch=amd64 jammy "$MNT" http://us.archive.ubuntu.com/ubuntu
echo "[3/7] Done"

# [4/7] Configure + packages
echo "[4/7] Configuring system..."
ROOT_UUID=$(blkid -s UUID -o value "$ROOT_LOOP")
EFI_UUID=$(blkid -s UUID -o value "$EFI_LOOP")

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
rm -f "$MNT/etc/resolv.conf"
printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" > "$MNT/etc/resolv.conf"

mkdir -p "$MNT/etc/netplan"
cat > "$MNT/etc/netplan/01-mfarm.yaml" <<'EOF'
network:
  version: 2
  renderer: networkd
  ethernets:
    all-en:
      match: { name: "en*" }
      dhcp4: true
      dhcp4-overrides:
        use-dns: true
        send-hostname: true
    all-eth:
      match: { name: "eth*" }
      dhcp4: true
      dhcp4-overrides:
        use-dns: true
        send-hostname: true
    # NOTE: do NOT add a wildcard `all-other: { driver: "*" }` block here.
    # systemd-networkd would match Docker's veth interfaces, try to manage
    # them as DHCP clients, and detach them from docker0 — breaking all
    # container networking (DNS times out inside containers, breaking
    # `docker pull`, breaking Vast's `Test docker` install step, etc.).
    # The all-en + all-eth matchers above already cover every real NIC.
EOF
chmod 600 "$MNT/etc/netplan/01-mfarm.yaml"

# Aggressive DHCP retry service - runs on boot, keeps trying until IP is obtained
cat > "$MNT/opt/mfarm/dhcp-forcer.sh" <<'DHCPSH'
#!/bin/bash
# Force DHCP on all ethernet interfaces at boot
# If DHCP times out in netplan, this retries more aggressively
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if ip -4 addr show 2>/dev/null | grep -qE 'inet (192|10|172)\.'; then
        logger "dhcp-forcer: got IP on attempt $attempt"
        exit 0
    fi
    for iface in $(ls /sys/class/net/ | grep -E '^(eth|en)' | head -5); do
        ip link set "$iface" up 2>/dev/null
        dhclient -4 -v "$iface" -timeout 30 2>&1 | logger -t dhcp-forcer &
    done
    sleep 10
done
exit 1
DHCPSH
chmod +x "$MNT/opt/mfarm/dhcp-forcer.sh"

cat > "$MNT/etc/systemd/system/dhcp-forcer.service" <<'DHCPSVC'
[Unit]
Description=Aggressive DHCP retry at boot
After=network.target systemd-networkd.service
Wants=network.target

[Service]
Type=oneshot
ExecStart=/opt/mfarm/dhcp-forcer.sh
RemainAfterExit=yes
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
DHCPSVC

mount --bind /dev "$MNT/dev" 2>/dev/null || true
mount --bind /dev/pts "$MNT/dev/pts" 2>/dev/null || true
mount -t proc proc "$MNT/proc" 2>/dev/null || true
mount -t sysfs sys "$MNT/sys" 2>/dev/null || true

# Install ALL packages including kernel with hardware drivers
cat > "$MNT/tmp/setup.sh" <<'SETUP'
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export HOME=/root
apt-get update -qq
# linux-generic pulls in image + headers + modules-extra as one package
apt-get install -y \
    linux-generic \
    systemd-sysv dbus \
    openssh-server python3 python3-venv \
    lm-sensors htop screen wget curl \
    net-tools pciutils usbutils \
    smartmontools sysstat nvme-cli \
    dkms build-essential \
    software-properties-common ubuntu-drivers-common \
    sudo locales iproute2 iputils-ping netplan.io \
    xserver-xorg-core xinit x11-xserver-utils \
    cloud-guest-utils gdisk isc-dhcp-client

# Verify r8169 is present
KVER=$(ls /lib/modules/ | sort -V | tail -1)
echo "Kernel: $KVER"
R8169=$(find /lib/modules/$KVER -name "r8169.ko*" | head -1)
echo "r8169: ${R8169:-NOT FOUND!}"
if [ -z "$R8169" ]; then
    echo "FATAL: r8169 not found after installing linux-modules-extra-generic"
    exit 1
fi

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
systemctl enable systemd-networkd

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

sed -i 's/PRETTY_NAME=.*/PRETTY_NAME="MeowOS"/' /etc/os-release
echo "SETUP_DONE"
SETUP
chmod +x "$MNT/tmp/setup.sh"
chroot "$MNT" /tmp/setup.sh

# NVIDIA drivers - separate step so base install survives if this fails
echo "  Installing NVIDIA driver 570..."
cat > "$MNT/tmp/nvidia-setup.sh" <<'NVSETUP'
#!/bin/bash
export DEBIAN_FRONTEND=noninteractive
add-apt-repository -y ppa:graphics-drivers/ppa
apt-get update -qq
apt-get install -y --no-install-recommends nvidia-driver-550 nvidia-settings libnvidia-compute-550 libnvidia-gl-550 ocl-icd-libopencl1 ocl-icd-opencl-dev
echo "NVIDIA: $(dpkg -l nvidia-driver-550 2>/dev/null | grep ^ii | awk '{print $3}')"
NVSETUP
chmod +x "$MNT/tmp/nvidia-setup.sh"
chroot "$MNT" /tmp/nvidia-setup.sh || echo "WARN: NVIDIA driver install failed (will retry on first boot)"

# Force-load common NIC drivers on boot
echo -e "r8169\ne1000e\nigb" > "$MNT/etc/modules-load.d/nic-drivers.conf"

# Blacklist nouveau (crashes journald on multi-GPU mining rigs)
cat > "$MNT/etc/modprobe.d/blacklist-nouveau.conf" <<'NOUVEAU'
blacklist nouveau
blacklist lbm-nouveau
options nouveau modeset=0
alias nouveau off
NOUVEAU
chroot "$MNT" update-initramfs -u 2>/dev/null || true

# Replace journald with /bin/true (hangs on debootstrap images)
mv "$MNT/lib/systemd/systemd-journald" "$MNT/lib/systemd/systemd-journald.real"
ln -s /bin/true "$MNT/lib/systemd/systemd-journald"

# Disable rsyslog entirely (with journald gone, rsyslog fills disk to 100%)
chroot "$MNT" systemctl disable rsyslog.service 2>/dev/null || true
chroot "$MNT" systemctl mask rsyslog.service 2>/dev/null || true

# Limit syslog/kern.log size (journald replacement causes rsyslog to fill disk)
cat > "$MNT/etc/logrotate.d/rsyslog-mining" <<'LOGROT'
/var/log/syslog /var/log/kern.log {
    size 50M
    rotate 2
    compress
    missingok
    notifempty
    postrotate
        /usr/lib/rsyslog/rsyslog-rotate
    endscript
}
LOGROT
# Also set rsyslog max file size as safety net
mkdir -p "$MNT/etc/rsyslog.d"
echo '$MaxMessageSize 4k' > "$MNT/etc/rsyslog.d/50-maxsize.conf"
echo '$SystemLogRateLimitBurst 200' >> "$MNT/etc/rsyslog.d/50-maxsize.conf"

echo "[4/7] Done"

# [5/7] MeowFarm agent + miners + web UI
echo "[5/7] Installing MeowFarm..."
mkdir -p "$MNT/opt/mfarm/miners" "$MNT/etc/mfarm" "$MNT/var/log/mfarm" "$MNT/var/run/mfarm"
mkdir -p "$MNT/root/.ssh"; chmod 700 "$MNT/root/.ssh"

cp "$SRC/mfarm/worker/mfarm-agent.py" "$MNT/opt/mfarm/mfarm-agent.py"
cp "$SRC/mfarm/worker/miner-wrapper.sh" "$MNT/opt/mfarm/miner-wrapper.sh"
cp "$SRC/mfarm/worker/mfarm-agent.service" "$MNT/etc/systemd/system/mfarm-agent.service"
chmod +x "$MNT/opt/mfarm/mfarm-agent.py" "$MNT/opt/mfarm/miner-wrapper.sh"

cp "$SRC/mfarm/worker/xmrig-1gb-hugepages.service" "$MNT/etc/systemd/system/xmrig-1gb-hugepages.service"
mkdir -p "$MNT/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/xmrig-1gb-hugepages.service \
    "$MNT/etc/systemd/system/multi-user.target.wants/xmrig-1gb-hugepages.service"

cp "$SRC/mfarm/worker/meowos-phonehome.py" "$MNT/opt/mfarm/meowos-phonehome.py"
cp "$SRC/mfarm/worker/meowos-phonehome.service" "$MNT/etc/systemd/system/meowos-phonehome.service"
chmod +x "$MNT/opt/mfarm/meowos-phonehome.py"

cp "$SRC/mfarm/worker/meowos-webui.py" "$MNT/opt/mfarm/meowos-webui.py"
cp "$SRC/mfarm/worker/meowos-webui.html" "$MNT/opt/mfarm/meowos-webui.html"
cp "$SRC/mfarm/worker/meowos-webui.service" "$MNT/etc/systemd/system/meowos-webui.service"
chmod +x "$MNT/opt/mfarm/meowos-webui.py"

# Auto-updater: rig polls console every 5 min for newer agent bundle
cp "$SRC/mfarm/worker/meowos-updater.py" "$MNT/opt/mfarm/meowos-updater.py"
cp "$SRC/mfarm/worker/meowos-updater.service" "$MNT/etc/systemd/system/meowos-updater.service"
cp "$SRC/mfarm/worker/meowos-updater.timer" "$MNT/etc/systemd/system/meowos-updater.timer"
chmod +x "$MNT/opt/mfarm/meowos-updater.py"

# Stamp VERSION onto the rig — updater compares this to console's /api/agent/version
cp "$SRC/VERSION" "$MNT/opt/mfarm/VERSION"

cp "$SRC/build-usb/mfarm-files/config.json" "$MNT/etc/mfarm/config.json"
chroot "$MNT" chown -R miner:miner /etc/mfarm /opt/mfarm /var/log/mfarm /var/run/mfarm

# 'miner' command shows live miner output
cat > "$MNT/usr/local/bin/miner" <<'MINERCMD'
#!/bin/bash
tail -n 50 -f /var/log/mfarm/miner.log
MINERCMD
chmod +x "$MNT/usr/local/bin/miner"

# Download all miners into image
cp "$SRC/mfarm/worker/miner-downloader.sh" "$MNT/opt/mfarm/miner-downloader.sh"
chmod +x "$MNT/opt/mfarm/miner-downloader.sh"
chroot "$MNT" bash /opt/mfarm/miner-downloader.sh all

# Also copy ccminer custom build if available
tar xzf "$SRC/ccminer-patch/hiveos/ccminer-6390-v21.1.1.tar.gz" -C /tmp 2>/dev/null && \
    cp /tmp/ccminer "$MNT/opt/mfarm/miners/ccminer" && \
    chmod +x "$MNT/opt/mfarm/miners/ccminer" || true

# CUDA runtime
cp "${MEOWOS_CUDA_DEB:-/mnt/c/Users/benef/libcudart12.deb}" "$MNT/tmp/libcudart12.deb" 2>/dev/null || echo "WARN: libcudart12.deb not found"
chroot "$MNT" bash -c 'dpkg -i --force-depends /tmp/libcudart12.deb 2>/dev/null; rm -f /tmp/libcudart12.deb' || true
echo '/usr/local/cuda-12.8/targets/x86_64-linux/lib' > "$MNT/etc/ld.so.conf.d/cuda-12.conf"
chroot "$MNT" ldconfig

# Firstboot script
cat > "$MNT/opt/mfarm/mfarm-firstboot.sh" <<'FB'
#!/bin/bash
set -uo pipefail
mkdir -p /var/run/mfarm /var/log/mfarm
LOG="/var/log/mfarm/firstboot.log"
MARKER="/opt/mfarm/.firstboot-done"
TOTAL=6
STEP=0

show() {
    STEP=$((STEP + 1))
    PCT=$((STEP * 100 / TOTAL))
    BAR=""
    FILL=$((PCT / 5))
    EMPTY=$((20 - FILL))
    for i in $(seq 1 $FILL); do BAR="${BAR}#"; done
    for i in $(seq 1 $EMPTY); do BAR="${BAR}-"; done
    clear
    echo ""
    echo "  MeowOS First-Boot Setup"
    echo "  [${BAR}] ${PCT}%"
    echo "  ${1}..."
    echo ""
    echo "$1" >> "$LOG"
}

exec 2>> "$LOG"

if [[ -f "$MARKER" ]]; then echo "Already done."; systemctl disable mfarm-firstboot.service; exit 0; fi

show "Generating SSH keys"
ssh-keygen -A >> "$LOG" 2>&1
su - miner -c 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519' >> "$LOG" 2>&1 || true

show "Expanding root partition"
ROOT_DEV=$(findmnt -n -o SOURCE /)
ROOT_DISK=$(echo "$ROOT_DEV" | sed 's/[0-9]*$//')
ROOT_PARTNUM=$(echo "$ROOT_DEV" | grep -o '[0-9]*$')
sgdisk -e "$ROOT_DISK" >> "$LOG" 2>&1 || true
growpart "$ROOT_DISK" "$ROOT_PARTNUM" >> "$LOG" 2>&1 || true
resize2fs "$ROOT_DEV" >> "$LOG" 2>&1 || true

show "Connecting to network"
for i in $(seq 1 30); do ping -c 1 -W 2 8.8.8.8 &>/dev/null && break; sleep 2; done

show "Configuring NVIDIA GPU"
if lspci | grep -qi nvidia; then
    nvidia-smi -pm 1 >> "$LOG" 2>&1 || true
    nvidia-xconfig --enable-all-gpus --cool-bits=31 --allow-empty-initial-configuration >> "$LOG" 2>&1 || true
fi
sensors-detect --auto >> "$LOG" 2>&1 || true

show "Configuring hostname"
MAC_SUFFIX=$(ip link show | grep -m1 "link/ether" | awk '{print $2}' | tr -d ':' | tail -c 5)
hostnamectl set-hostname "mfarm-rig-${MAC_SUFFIX}"
sed -i "s/127.0.1.1.*/127.0.1.1\tmfarm-rig-${MAC_SUFFIX}/" /etc/hosts
# Second nvidia-xconfig call (the first is inside the lspci-guard above).
# Guard this one too — it logged "ERROR: Unable to determine number of
# GPUs in system" on CPU-only rigs and confused users into thinking the
# install was broken.
if lspci | grep -qi nvidia; then
    nvidia-xconfig --enable-all-gpus --cool-bits=31 --allow-empty-initial-configuration >> "$LOG" 2>&1 || true
fi

show "Starting MeowOS services"
systemctl enable mfarm-agent >> "$LOG" 2>&1
systemctl enable meowos-phonehome.service >> "$LOG" 2>&1
systemctl enable meowos-webui.service >> "$LOG" 2>&1
systemctl start meowos-phonehome.service >> "$LOG" 2>&1
systemctl start meowos-webui.service >> "$LOG" 2>&1

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

show "Setup complete! Rebooting"
touch "$MARKER"
systemctl disable mfarm-firstboot.service
sleep 5
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
chroot "$MNT" systemctl enable dhcp-forcer.service 2>/dev/null || true
chroot "$MNT" systemctl enable meowos-updater.timer 2>/dev/null || true

# Cleanup
chroot "$MNT" apt-get clean
rm -rf "$MNT/var/lib/apt/lists/"* "$MNT/tmp/"*

echo "[5/7] Done"

# [6/7] Systemd-boot
echo "[6/7] Installing boot..."
KERNEL=$(ls "$MNT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | xargs -r basename)
INITRD=$(ls "$MNT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | xargs -r basename)

if [ -z "$KERNEL" ]; then echo "FATAL: No kernel"; ls "$MNT/boot/"; exit 1; fi
if [ -z "$INITRD" ]; then
    KVER="${KERNEL#vmlinuz-}"
    chroot "$MNT" update-initramfs -c -k "$KVER" 2>/dev/null || true
    INITRD=$(ls "$MNT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | xargs -r basename)
fi
echo "  Kernel: $KERNEL  Initrd: $INITRD  Root: $ROOT_UUID"

mkdir -p "$MNT/boot/efi/EFI/BOOT" "$MNT/boot/efi/loader/entries"
cp "$MNT/usr/lib/systemd/boot/efi/systemd-bootx64.efi" "$MNT/boot/efi/EFI/BOOT/BOOTX64.EFI"
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

echo "[6/7] Done"

# [7/7] Finalize
echo "[7/7] Finalizing..."
umount "$MNT/dev/pts" "$MNT/dev" "$MNT/proc" "$MNT/sys" 2>/dev/null || true
umount "$MNT/boot/efi" "$MNT" 2>/dev/null || true

# Final filesystem check
e2fsck -f -y "$ROOT_LOOP" || true

losetup -d "$EFI_LOOP" "$ROOT_LOOP" 2>/dev/null || true
sync

if [ "$IMG" != "$OUTPUT" ]; then
    cp "$IMG" "$OUTPUT"; rm -f "$IMG"
else
    echo "Image at $OUTPUT"
fi

echo ""
echo "============================================"
echo "  MeowOS v$VERSION built successfully!"
echo "============================================"
