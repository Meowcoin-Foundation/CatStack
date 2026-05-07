#!/bin/bash
# meowos-dpkg-health.sh — detect a corrupted /var/lib/dpkg/status and
# self-heal from the most recent intact backup. Runs on every boot before
# mfarm-agent.service.
#
# Failure mode this catches: the dpkg status file ends up "right size, all
# NULL bytes" after an unclean shutdown — ext4 committed the inode but not
# the data block. apt then refuses every operation with "could not parse
# /var/lib/dpkg/status" and any installer that touches packages (Vast,
# CUDA bumps, agent self-update) falls over until manually fixed.
#
# Recovery sources, in order of trustworthiness:
#   /var/lib/dpkg/status-old           dpkg's own atomic backup, written
#                                      before each rename of status. Most
#                                      recent known-good in 99% of cases.
#   /var/backups/dpkg.status.0/1/2     daily logrotate snapshots. Used when
#                                      status-old got hit too (rare).

set -uo pipefail

LOG=/var/log/mfarm/dpkg-health.log
mkdir -p "$(dirname "$LOG")"
exec >> "$LOG" 2>&1

echo
echo "=== $(date) dpkg health check ==="

if dpkg-query --list >/dev/null 2>&1; then
    echo "  status OK"
    exit 0
fi

echo "  status CORRUPT — attempting auto-recovery"
ls -la /var/lib/dpkg/status /var/lib/dpkg/status-old /var/backups/dpkg.status.* 2>/dev/null

# Save the broken file once so an operator can post-mortem.
cp -p /var/lib/dpkg/status "/var/lib/dpkg/status.corrupt-$(date +%s)" 2>/dev/null || true

for src in /var/lib/dpkg/status-old \
           /var/backups/dpkg.status.0 \
           /var/backups/dpkg.status.1 \
           /var/backups/dpkg.status.2; do
    [[ -s "$src" ]] || { echo "  skip $src — missing or empty"; continue; }
    pkg_count=$(grep -c '^Package: ' "$src" 2>/dev/null || echo 0)
    if [[ "$pkg_count" -lt 50 ]]; then
        echo "  skip $src — only $pkg_count packages (looks corrupt too)"
        continue
    fi
    echo "  trying $src ($pkg_count packages)"
    cp -p "$src" /var/lib/dpkg/status
    if dpkg-query --list >/dev/null 2>&1; then
        echo "  RECOVERED from $src"
        # Reconcile any half-configured packages so apt is fully usable.
        # No-op when everything is already configured.
        dpkg --configure -a >/dev/null 2>&1 || \
            echo "  WARN: dpkg --configure -a returned non-zero"
        exit 0
    fi
    echo "  $src didn't make dpkg happy — trying next"
done

echo "  RECOVERY FAILED — all backup sources broken or missing"
echo "  Original corrupt file preserved at /var/lib/dpkg/status.corrupt-*"
exit 1
