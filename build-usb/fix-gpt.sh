#!/bin/bash
# Fix GPT backup header on the target SSD after dd
# Usage: sudo bash fix-gpt.sh <min_gb> <max_gb>
set -euo pipefail
MIN=${1:-100}
MAX=${2:-130}

for dev in /dev/sd?; do
    [ -b "$dev" ] || continue
    SIZE=$(blockdev --getsize64 "$dev" 2>/dev/null || echo 0)
    GB=$((SIZE / 1073741824))
    if [ "$GB" -ge "$MIN" ] && [ "$GB" -le "$MAX" ]; then
        echo "Fixing GPT on $dev ($GB GB)..."
        sgdisk -e "$dev"
        sgdisk -v "$dev" 2>&1 | head -3
        echo "GPT fixed."
        exit 0
    fi
done
echo "ERROR: No disk found in ${MIN}-${MAX}GB range"
exit 1
