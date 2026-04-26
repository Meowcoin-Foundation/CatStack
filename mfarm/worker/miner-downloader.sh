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

    # Detect archive type from magic bytes (NOT `file` — minimal MeowOS images
    # don't ship `file` and the downloader silently fell through to `mv archive
    # "$binary"`, leaving a still-gzipped blob that fails with "Exec format error"
    # at miner launch time. Observed on mini01 with xmrig.tar.gz on 2026-04-26.)
    local magic
    magic=$(head -c 4 archive 2>/dev/null | od -An -tx1 | tr -d ' \n')
    case "$magic" in
        1f8b*)             tar xzf archive 2>/dev/null ;;            # gzip
        504b0304|504b0506) unzip -q archive 2>/dev/null ;;           # zip
        fd377a58|377abcaf) tar xJf archive 2>/dev/null || tar xf archive 2>/dev/null ;;  # xz / 7z
        4c5a4950)          tar xf archive 2>/dev/null ;;             # lzip
        425a68*)           tar xjf archive 2>/dev/null ;;            # bzip2
        7f454c46|cffaedfe) mv archive "$binary" ;;                   # ELF / Mach-O — raw binary
        *)
            # Unknown magic: fall back to extension-based detection
            case "$url" in
                *.tar.gz|*.tgz)  tar xzf archive 2>/dev/null ;;
                *.tar.xz|*.txz)  tar xJf archive 2>/dev/null ;;
                *.tar.bz2|*.tbz) tar xjf archive 2>/dev/null ;;
                *.tar)           tar xf archive 2>/dev/null ;;
                *.zip)           unzip -q archive 2>/dev/null ;;
                *)               mv archive "$binary" ;;
            esac
            ;;
    esac

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
