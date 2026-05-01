#!/bin/bash
#
# MFarm USB Installer Builder
# Run this in WSL: bash /mnt/c/Source/mfarm/build-usb/build.sh
#
# Produces: /mnt/c/Source/mfarm/build-usb/mfarm-installer.iso
#
set -euo pipefail

BUILD_DIR="/tmp/mfarm-iso-build"
SOURCE_DIR="/mnt/c/Source/mfarm/build-usb"
OUTPUT_ISO="/mnt/c/Source/mfarm/build-usb/mfarm-installer.iso"
UBUNTU_ISO_URL="https://releases.ubuntu.com/22.04.5/ubuntu-22.04.5-live-server-amd64.iso"
UBUNTU_ISO="$BUILD_DIR/ubuntu-server.iso"
MEMTEST_VER="7.20"
MEMTEST_URL="https://memtest.org/download/v${MEMTEST_VER}/mt86plus_${MEMTEST_VER}.binaries.zip"
MEMTEST_ZIP="$BUILD_DIR/memtest86plus.zip"
MEMTEST_DIR="$BUILD_DIR/memtest"

echo "============================================"
echo "  MFarm USB Installer Builder"
echo "============================================"
echo ""

# ── 1. Install required tools ────────────────────────────────────────

echo "[1/6] Installing build tools..."
sudo apt-get update -qq
sudo apt-get install -y -qq xorriso p7zip-full wget unzip

# ── 2. Download Ubuntu Server ISO ────────────────────────────────────

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

if [[ -f "$UBUNTU_ISO" ]]; then
    echo "[2/6] Ubuntu ISO already downloaded, reusing..."
else
    echo "[2/6] Downloading Ubuntu Server 22.04.5 (~2GB)..."
    wget -q --show-progress -O "$UBUNTU_ISO" "$UBUNTU_ISO_URL"
fi

# ── 2b. Download Memtest86+ binaries ─────────────────────────────────

if [[ -f "$MEMTEST_ZIP" ]]; then
    echo "      Memtest86+ already downloaded, reusing..."
else
    echo "      Downloading Memtest86+ ${MEMTEST_VER}..."
    wget -q -O "$MEMTEST_ZIP" "$MEMTEST_URL"
fi

rm -rf "$MEMTEST_DIR"
mkdir -p "$MEMTEST_DIR"
unzip -q -o "$MEMTEST_ZIP" -d "$MEMTEST_DIR"

# ── 3. Extract ISO contents ─────────────────────────────────────────

echo "[3/6] Extracting ISO..."
rm -rf "$BUILD_DIR/iso"
mkdir -p "$BUILD_DIR/iso"

# Extract using xorriso (preserves boot catalog info)
xorriso -osirrox on -indev "$UBUNTU_ISO" -extract / "$BUILD_DIR/iso" 2>/dev/null

# Make everything writable so we can modify
chmod -R u+w "$BUILD_DIR/iso"

# ── 4. Inject MFarm files ───────────────────────────────────────────

echo "[4/6] Injecting MFarm files..."

# Add autoinstall config
mkdir -p "$BUILD_DIR/iso/autoinstall"
cp "$SOURCE_DIR/autoinstall/user-data" "$BUILD_DIR/iso/autoinstall/user-data"
cp "$SOURCE_DIR/autoinstall/meta-data" "$BUILD_DIR/iso/autoinstall/meta-data"

# Add MFarm files (referenced by autoinstall late-commands)
mkdir -p "$BUILD_DIR/iso/mfarm-files"
cp "$SOURCE_DIR/mfarm-files/"* "$BUILD_DIR/iso/mfarm-files/"

# Add Memtest86+ binaries (BIOS .bin + UEFI .efi)
mkdir -p "$BUILD_DIR/iso/boot/memtest"
cp "$MEMTEST_DIR/memtest64.bin" "$BUILD_DIR/iso/boot/memtest/memtest.bin"
cp "$MEMTEST_DIR/memtest64.efi" "$BUILD_DIR/iso/boot/memtest/memtest.efi"

# Modify GRUB config to enable autoinstall
# The key is adding 'autoinstall' to the kernel command line
# and pointing to our cloud-init datasource
GRUB_CFG="$BUILD_DIR/iso/boot/grub/grub.cfg"
if [[ -f "$GRUB_CFG" ]]; then
    echo "  Modifying GRUB config for autoinstall..."
    # Replace the first menuentry's linux line to add autoinstall
    # The default Ubuntu grub.cfg has: linux /casper/vmlinuz ---
    # We change it to: linux /casper/vmlinuz autoinstall ds=nocloud\;s=/cdrom/autoinstall/ ---
    sed -i 's|linux\t/casper/vmlinuz  ---|linux\t/casper/vmlinuz autoinstall ds=nocloud\\;s=/cdrom/autoinstall/ ---|' "$GRUB_CFG"

    # Also set a short timeout so it boots automatically
    sed -i 's/set timeout=30/set timeout=10/' "$GRUB_CFG"
    sed -i 's/set timeout=-1/set timeout=10/' "$GRUB_CFG"

    # Append Memtest86+ menuentry (works for both BIOS and UEFI)
    cat >> "$GRUB_CFG" <<'GRUB_MEMTEST'

menuentry "Memtest86+ (RAM diagnostic)" {
    if [ "${grub_platform}" = "efi" ]; then
        chainloader /boot/memtest/memtest.efi
    else
        linux16 /boot/memtest/memtest.bin
    fi
}
GRUB_MEMTEST
fi

# Also modify the BIOS/legacy boot config (isolinux/syslinux)
ISOLINUX_CFG="$BUILD_DIR/iso/isolinux/txt.cfg"
if [[ -f "$ISOLINUX_CFG" ]]; then
    echo "  Modifying ISOLINUX config for autoinstall..."
    sed -i 's|append   initrd=/casper/initrd  ---|append   initrd=/casper/initrd autoinstall ds=nocloud;s=/cdrom/autoinstall/  ---|' "$ISOLINUX_CFG"
fi

# Set default boot option and timeout for isolinux
ISOLINUX_MAIN="$BUILD_DIR/iso/isolinux/isolinux.cfg"
if [[ -f "$ISOLINUX_MAIN" ]]; then
    sed -i 's/timeout 0/timeout 50/' "$ISOLINUX_MAIN"  # 5 seconds
fi

echo "  Autoinstall config injected"
echo "  MFarm files: $(ls -1 $BUILD_DIR/iso/mfarm-files/ | wc -l) files"

# ── 5. Repack ISO ───────────────────────────────────────────────────

echo "[5/6] Repacking ISO..."

# Use the exact boot parameters extracted from the original ISO.
# The EFI partition is read directly from the original ISO (--interval syntax),
# and the MBR boot code is also sourced from it.
xorriso -as mkisofs \
    -r -V "MFarm Installer" \
    -o "$OUTPUT_ISO" \
    --grub2-mbr --interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:"$UBUNTU_ISO" \
    --protective-msdos-label \
    -partition_cyl_align off \
    -partition_offset 16 \
    --mbr-force-bootable \
    -append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b --interval:local_fs:4162948d-4173019d::"$UBUNTU_ISO" \
    -appended_part_as_gpt \
    -iso_mbr_part_type a2a0d0ebe5b9334487c068b6b72699c7 \
    -c '/boot.catalog' \
    -b '/boot/grub/i386-pc/eltorito.img' \
    -no-emul-boot -boot-load-size 4 -boot-info-table --grub2-boot-info \
    -eltorito-alt-boot \
    -e '--interval:appended_partition_2_start_1040737s_size_10072d:all::' \
    -no-emul-boot -boot-load-size 10072 \
    "$BUILD_DIR/iso" \
    2>&1 | tail -5

# ── 6. Verify ───────────────────────────────────────────────────────

echo "[6/6] Verifying..."

if [[ -f "$OUTPUT_ISO" ]]; then
    SIZE=$(du -h "$OUTPUT_ISO" | awk '{print $1}')
    echo ""
    echo "============================================"
    echo "  MFarm Installer ISO Built Successfully!"
    echo "============================================"
    echo "  Output: C:\\Source\\mfarm\\build-usb\\mfarm-installer.iso"
    echo "  Size:   $SIZE"
    echo ""
    echo "  Flash this ISO to a USB drive using balenaEtcher."
    echo "  Then boot a rig from the USB to install."
    echo ""
    echo "  What happens when you boot:"
    echo "    1. Ubuntu installs automatically (~10 min)"
    echo "    2. First boot provisions NVIDIA drivers (~5 min)"
    echo "    3. Rig reboots, MFarm agent starts"
    echo "    4. Add the rig: mfarm rig add <name> <ip>"
    echo "============================================"
else
    echo "ERROR: ISO creation failed!"
    exit 1
fi

# Cleanup extracted files (keep the downloaded ISO for re-use)
echo ""
echo "Cleaning up build artifacts..."
rm -rf "$BUILD_DIR/iso" "$BUILD_DIR/mbr.bin"
echo "Done. (Original Ubuntu ISO kept at $UBUNTU_ISO for future rebuilds)"
