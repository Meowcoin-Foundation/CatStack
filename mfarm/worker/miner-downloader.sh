#!/bin/bash
# Download/update mining binaries to /opt/mfarm/miners/
# Usage: bash miner-downloader.sh [miner_name|all]
set -uo pipefail

DEST="/opt/mfarm/miners"
mkdir -p "$DEST"
TARGET="${1:-all}"

download() {
    local name="$1" url="$2" binary="$3" strip="${4:-0}"
    if [ "$TARGET" != "all" ] && [ "$TARGET" != "$name" ]; then return; fi
    echo "  Downloading $name..."
    cd /tmp
    rm -rf "dl-$name"; mkdir "dl-$name"; cd "dl-$name"
    wget -q --show-progress "$url" -O archive 2>&1 || { echo "  FAILED: $name"; return; }

    # Extract based on file type
    if file archive | grep -q "gzip"; then
        tar xzf archive 2>/dev/null
    elif file archive | grep -q "Zip\|zip"; then
        unzip -q archive 2>/dev/null
    elif file archive | grep -q "xz"; then
        tar xJf archive 2>/dev/null
    else
        mv archive "$binary"
    fi

    # Find the binary
    BIN=$(find . -name "$binary" -type f | head -1)
    if [ -z "$BIN" ]; then
        BIN=$(find . -type f -executable | head -1)
    fi
    if [ -n "$BIN" ]; then
        cp "$BIN" "$DEST/$binary"
        chmod +x "$DEST/$binary"
        echo "  OK: $DEST/$binary ($(ls -lh "$DEST/$binary" | awk '{print $5}'))"
    else
        echo "  FAILED: binary '$binary' not found in archive"
        ls -la
    fi
    cd /tmp; rm -rf "dl-$name"
}

echo "=== MeowOS Miner Downloader ==="

# T-Rex (NVIDIA)
download "trex" \
    "https://github.com/trexminer/T-Rex/releases/download/0.26.8/t-rex-0.26.8-linux.tar.gz" \
    "t-rex"

# lolMiner (AMD/NVIDIA)
download "lolminer" \
    "https://github.com/Lolliedieb/lolMiner-releases/releases/download/1.92/lolMiner_v1.92_Lin64.tar.gz" \
    "lolMiner"

# miniZ (NVIDIA)
download "miniz" \
    "https://github.com/miniZ-miner/miniZ/releases/download/v2.5e3/miniZ_v2.5e3_linux-x64.tar.gz" \
    "miniZ"

# XMRig (CPU/GPU)
download "xmrig" \
    "https://github.com/xmrig/xmrig/releases/download/v6.22.2/xmrig-6.22.2-linux-static-x64.tar.gz" \
    "xmrig"

# CPUMiner-Opt (CPU)
download "cpuminer-opt" \
    "https://github.com/JayDDee/cpuminer-opt/releases/download/v24.4/cpuminer-opt-24.4-linux-x86_64.tar.gz" \
    "cpuminer"

# SRBMiner-Multi (AMD/CPU)
download "srbminer" \
    "https://github.com/doktor83/SRBMiner-Multi/releases/download/2.8.3/SRBMiner-Multi-2-8-3-Linux.tar.xz" \
    "SRBMiner-Multi"

# Rigel (NVIDIA — xelishash, autolykos2, kaspa, ironfish, etc.)
download "rigel" \
    "https://github.com/rigelminer/rigel/releases/download/1.23.2/rigel-1.23.2-linux.tar.gz" \
    "rigel"

echo ""
echo "=== Installed miners ==="
ls -lh "$DEST/"
