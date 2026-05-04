#!/bin/bash
# Download/update mining binaries to /opt/mfarm/miners/
# Usage: bash miner-downloader.sh [miner_name|all]
set -uo pipefail

DEST="/opt/mfarm/miners"
mkdir -p "$DEST"
TARGET="${1:-all}"

FAIL_COUNT=0

download() {
    # Args: name, url, install_name, [archive_binary]
    #   install_name = filename to write at $DEST (what mfarm-agent looks for)
    #   archive_binary = name to find inside the archive, defaults to install_name
    #     (needed when archive has a different-cased or version-suffixed binary,
    #      e.g. SRBMiner-Multi v3.x ships as SRBMiner-Multi-3-2-7/SRBMiner-MULTI)
    local name="$1" url="$2" install_name="$3" archive_binary="${4:-$3}"
    if [ "$TARGET" != "all" ] && [ "$TARGET" != "$name" ]; then return; fi
    echo "  Downloading $name from $url"
    cd /tmp
    rm -rf "dl-$name"; mkdir "dl-$name"; cd "dl-$name"
    if ! wget -q --show-progress "$url" -O archive 2>&1; then
        echo "  FAILED: $name — wget error" >&2
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return
    fi
    # Catch silent 404s — github returns a 9-byte text body. Anything under 1KB
    # for a miner archive is suspect.
    local sz
    sz=$(stat -c%s archive 2>/dev/null || wc -c < archive)
    if [ "$sz" -lt 1024 ]; then
        echo "  FAILED: $name — archive too small ($sz bytes; likely 404 or release retracted)" >&2
        head -c 200 archive >&2; echo >&2
        FAIL_COUNT=$((FAIL_COUNT + 1))
        cd /tmp; rm -rf "dl-$name"
        return
    fi

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
        7f454c46|cffaedfe) mv archive "$install_name" ;;             # ELF / Mach-O — raw binary
        *)
            # Unknown magic: fall back to extension-based detection
            case "$url" in
                *.tar.gz|*.tgz)  tar xzf archive 2>/dev/null ;;
                *.tar.xz|*.txz)  tar xJf archive 2>/dev/null ;;
                *.tar.bz2|*.tbz) tar xjf archive 2>/dev/null ;;
                *.tar)           tar xf archive 2>/dev/null ;;
                *.zip)           unzip -q archive 2>/dev/null ;;
                *)               mv archive "$install_name" ;;
            esac
            ;;
    esac

    # Locate the binary inside the extracted tree.
    # 1. Exact match for the archive's binary name.
    # 2. Fallback: largest executable file >1MB (skip .sh wrapper scripts).
    BIN=$(find . -name "$archive_binary" -type f | head -1)
    if [ -z "$BIN" ]; then
        BIN=$(find . -type f -executable -size +1M -printf '%s %p\n' 2>/dev/null \
              | sort -rn | head -1 | cut -d' ' -f2-)
    fi
    if [ -n "$BIN" ] && [ -s "$BIN" ]; then
        cp "$BIN" "$DEST/$install_name"
        chmod +x "$DEST/$install_name"
        echo "  OK: $DEST/$install_name ($(ls -lh "$DEST/$install_name" | awk '{print $5}'))"
    else
        echo "  FAILED: $name — binary '$archive_binary' not found in archive" >&2
        ls -la >&2
        FAIL_COUNT=$((FAIL_COUNT + 1))
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

# SRBMiner-Multi (AMD/CPU). Archive layout from v3.x: SRBMiner-Multi-N-N-N/SRBMiner-MULTI
# (uppercase MULTI, in versioned subdir). 4th arg is the binary name inside the tarball;
# we install it as "SRBMiner-Multi" to match what mfarm-agent.py looks for.
download "srbminer" \
    "https://github.com/doktor83/SRBMiner-Multi/releases/download/3.2.7/SRBMiner-Multi-3-2-7-Linux.tar.gz" \
    "SRBMiner-Multi" \
    "SRBMiner-MULTI"

# Rigel (NVIDIA — xelishash, autolykos2, kaspa, ironfish, etc.)
download "rigel" \
    "https://github.com/rigelminer/rigel/releases/download/1.23.2/rigel-1.23.2-linux.tar.gz" \
    "rigel"

echo ""
echo "=== Installed miners ==="
ls -lh "$DEST/"

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo "" >&2
    echo "=== $FAIL_COUNT miner(s) failed to install — see errors above ===" >&2
    exit 1
fi
