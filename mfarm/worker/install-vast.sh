#!/usr/bin/env bash
# install-vast.sh — Vast.ai host daemon install for MeowOS rigs
#
# Bakes in every gotcha catalogued in reference_vastai_install.md so a fresh
# rig goes from "MeowOS shipping defaults" to "Vast daemon active and reporting
# to the controller" in one shot, with no interactive conffile prompts and no
# tokens burned to surprise failures.
#
# Usage:
#   sudo bash install-vast.sh <TOKEN> [PORT_BASE]
#
#   TOKEN     — single-use install token from https://cloud.vast.ai/host/setup/
#               (consumed at first /api/v0/daemon/identify/ POST — get a fresh
#               one from the portal before each run, do not reuse)
#   PORT_BASE — first 2 digits of the port range, default 41 → opens 41000-41200.
#               Must be unique per rig if multiple rigs share a public IP
#               (they NAT to one router); pick 42 for the next rig, etc.
#
# Run this on the rig, not from the console. SCP it over first, e.g.:
#   scp install-vast.sh root@<rig-ip>:/tmp/
#   ssh root@<rig-ip> "bash /tmp/install-vast.sh ABC123 42"

set -uo pipefail

# ── Args ───────────────────────────────────────────────────────────────
TOKEN="${1:-}"
PORT_BASE="${2:-41}"

if [[ -z "$TOKEN" ]]; then
    read -rp "Vast install token: " TOKEN
fi
[[ -n "$TOKEN" ]] || { echo "ERROR: install token required" >&2; exit 1; }
[[ "$PORT_BASE" =~ ^[0-9]+$ ]] || { echo "ERROR: port-base must be numeric" >&2; exit 1; }

PORT_START="${PORT_BASE}000"
PORT_END="${PORT_BASE}200"
LOG=/var/log/vast-install.log

say()  { echo -e "\n=== $* ==="; }
warn() { echo "WARN: $*" >&2; }
die()  { echo "FATAL: $*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || die "must run as root (try: sudo bash $0 $*)"

# ── 1/10  Pre-flight ───────────────────────────────────────────────────
say "1/10  pre-flight checks"

# Pause CPU mining BEFORE the network checks below. xmrig pinning all cores
# (load avg 25+ on a 32-core mini PC) makes the 8s curl timeout fire on DNS
# or TLS handshake even though the network itself is fine — pre-flight then
# dies with "cannot reach console.vast.ai". Stop now; step 3 still does the
# full mask/stop for everything else, step 10 still restores mfarm-agent.
systemctl stop mfarm-agent 2>/dev/null || true
pkill -9 xmrig 2>/dev/null || true

# GPU must be on the PCIe bus — kaalia FATALs without one and burns the token.
# Note: must NOT use `grep -q` here. With `set -o pipefail` (above), grep -q's
# early exit on first match closes the pipe, lspci dies with SIGPIPE (exit 141),
# and pipefail propagates the 141 — making the `! pipe` test fire die() even
# when the GPU is present and grep matched. Use `grep ... >/dev/null` instead.
if ! lspci -nn | grep -iE 'vga|3d.*nvidia|nvidia.*controller' >/dev/null; then
    die "no NVIDIA GPU on PCIe bus. Reseat the card / verify 12VHPWR fully clicked / enable 'Above 4G Decoding' in BIOS, then retry."
fi
if ! nvidia-smi -q >/dev/null 2>&1; then
    echo "  nvidia driver not communicating, attempting modprobe..."
    nvidia-modprobe -A >/dev/null 2>&1 || modprobe nvidia
    nvidia-smi -q >/dev/null 2>&1 || die "nvidia driver still not responding. Reboot or fix driver before continuing."
fi

# Network sanity — both endpoints the install hits.
curl -sf --max-time 8 -o /dev/null https://console.vast.ai/install \
    || die "cannot reach console.vast.ai (network or DNS issue)"
curl -sf --max-time 8 -o /dev/null https://download.docker.com/linux/ubuntu/dists/jammy/InRelease \
    || warn "download.docker.com slow/unreachable — pre-cache step may take longer"

# Mask the daily-update timers up front so they can't kick off mid-install.
# The full mask (and stop of any service this script touches) happens in
# step 3, but those firing between step 1 and step 3 has bitten us — they
# hold the apt lock long enough to make `apt-get update` fail silently and
# every subsequent `apt install` fail with "Unable to locate package".
for t in apt-daily.timer apt-daily-upgrade.timer; do
    systemctl stop "$t" 2>/dev/null || true
    systemctl mask "$t" >/dev/null 2>&1 || true
done

# Wait up to 60s for any in-flight apt/dpkg to release the lock — same
# motivation as above. Once the lock is held by an unattended-upgrades
# run we already missed, all we can do is wait it out.
for _ in $(seq 1 30); do
    if ! pgrep -x apt-get >/dev/null 2>&1 && ! pgrep -x dpkg >/dev/null 2>&1 \
       && ! pgrep -f unattended-upgrade >/dev/null 2>&1; then
        break
    fi
    echo "  waiting for in-flight apt/dpkg to finish..."
    sleep 2
done

# Refresh apt cache before installing tools — without this, fresh-flashed
# images can have stale package references.
apt-get update -qq 2>&1 | tail -3 || warn "apt-get update produced warnings"

# MeowOS images install cuda-cudart-12-8 via `dpkg -i --force-depends` (no
# CUDA repo configured), which leaves apt's dependency tree partly broken —
# subsequent `apt-get install` calls fail with "Unmet dependencies" because
# the cuda-cudart deps (cuda-toolkit-config-common etc.) aren't installable.
# Earlier versions of this script ran `apt-get -f install -y` here, which
# resolved the unmet deps by REMOVING cuda-cudart-12-8. That's fine for a
# pure Vast host (Vast bakes its own CUDA into containers), but our rigs
# also run ccminer on the host — and ccminer needs libcudart.so.12. Losing
# it bricked mining on mini09 last install.
#
# Safer approach: do a dry-run of -f install, refuse if it would remove
# anything, fall back to `dpkg --configure -a` (which fixes only pending
# configurations, doesn't touch the install set). Downstream apt-get install
# steps will fail with specific errors if the dep tree is too broken to
# proceed; that's better than silently nuking ccminer.
plan=$(apt-get -f install -s 2>&1 || true)
if echo "$plan" | grep -qE '^Remv '; then
    say "apt-get -f install would REMOVE the following — refusing"
    echo "$plan" | grep -E '^Remv ' | sed 's/^/    /'
    say "running dpkg --configure -a instead (no removals)"
    dpkg --configure -a 2>&1 | tail -3 || warn "dpkg --configure -a warnings"
else
    apt-get -f install -y 2>&1 | tail -3 || warn "apt-get -f install warnings (may be cosmetic)"
fi

# Tools the script uses. python3-pip is essential as a fallback path for
# python3-requests when apt has unresolvable conflicts.
for pkg in rsync screen python3 python3-pip python3-requests gdisk parted; do
    dpkg -s "$pkg" >/dev/null 2>&1 \
        || apt-get install -y -qq "$pkg" 2>&1 | tail -3 \
        || warn "apt install $pkg failed (will fall back if critical)"
done

# The Vast python installer (downloaded in step 7) hard-imports `requests` at
# its top — if that import fails, step 8 crashes immediately and the user has
# no idea why. Verify it's importable; pip-fallback if apt couldn't deliver it.
if ! python3 -c 'import requests' 2>/dev/null; then
    echo "  python3-requests not importable — installing via pip"
    # Try ensurepip first in case pip module isn't installed (python3-venv
    # ships ensurepip but not pip itself on some Ubuntu builds).
    python3 -m ensurepip --upgrade 2>&1 | tail -2 || true
    python3 -m pip install --break-system-packages --quiet requests 2>&1 | tail -3 \
        || python3 -m pip install --quiet requests 2>&1 | tail -3 \
        || die "could not install python3 'requests' module via apt OR pip. Fix python3 environment then retry."
    python3 -c 'import requests' >/dev/null 2>&1 \
        || die "'requests' still not importable after pip install. Investigate."
fi

GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
echo "  GPU: $GPU_NAME"

# ── 2/10  Resize root partition to fill the disk ───────────────────────
say "2/10  partition / filesystem"

ROOT_DEV=$(findmnt -no SOURCE /)              # /dev/nvme0n1p2
DISK_DEV="/dev/$(lsblk -no PKNAME "$ROOT_DEV")"
PART_NUM=$(grep -oE '[0-9]+$' <<<"$ROOT_DEV")

# Free space at end of disk, in GiB
FREE_GB=$(parted -s "$DISK_DEV" unit GiB print free 2>/dev/null \
    | awk '/Free Space/ { gsub("GiB","",$3); print int($3) }' | tail -1)

if (( ${FREE_GB:-0} > 5 )); then
    echo "  ${FREE_GB} GiB unallocated at end of $DISK_DEV — growing partition $PART_NUM"
    sgdisk -e "$DISK_DEV" >/dev/null
    echo Yes | parted "$DISK_DEV" ---pretend-input-tty resizepart "$PART_NUM" 100% >/dev/null 2>&1
    partprobe "$DISK_DEV"
    sleep 1
    resize2fs "$ROOT_DEV" >/dev/null
    df -h /
else
    echo "  partition already covers full disk ($FREE_GB GiB free unallocated)"
fi

# ── 3/10  Mask / pause conflicting services ───────────────────────────
say "3/10  pause CPU miner, mask competitors"
# Two categories:
#   KILL_SVCS — masked permanently because they fight the install OR run
#     duplicate workloads (meowos-xmrig is a redundant standalone xmrig that
#     would compete with mfarm-agent's flight-sheet-managed xmrig).
#   PAUSE_SVCS — stopped during install, RESTORED at step 10. This preserves
#     CPU mining (xmrig via mfarm-agent) on Vast hosts: user wants xmrig
#     running even during Vast rentals so CPU isn't idle, but during the
#     install itself it would steal cores from `apt-get install` and `docker
#     pull` — so we suspend it for the install duration only.
# `disable` does NOT survive a reboot for masked services — must MASK. If
# unit file is in /etc/systemd/system/ (admin-installed), `mask` fails
# until the unit file is removed.
KILL_SVCS=(
    meowos-xmrig
    meowos-phonehome
    meowos-webui
    apt-daily.timer
    apt-daily-upgrade.timer
    apt-daily.service
    apt-daily-upgrade.service
    unattended-upgrades.service
)
PAUSE_SVCS=(
    mfarm-agent
)
for svc in "${KILL_SVCS[@]}"; do
    systemctl stop "$svc" 2>/dev/null
    rm -f "/etc/systemd/system/$svc" \
          "/etc/systemd/system/multi-user.target.wants/$svc" \
          "/etc/systemd/system/timers.target.wants/$svc"
done
systemctl daemon-reload
for svc in "${KILL_SVCS[@]}"; do
    systemctl mask "$svc" >/dev/null 2>&1
done
for svc in "${PAUSE_SVCS[@]}"; do
    systemctl stop "$svc" 2>/dev/null || true
done
pkill -9 xmrig 2>/dev/null || true
echo "  masked: ${KILL_SVCS[*]}"
echo "  paused (will restore at step 10): ${PAUSE_SVCS[*]}"

# ── 4/10  Preserve env across sudo (kills conffile prompts) ────────────
say "4/10  /etc/sudoers.d/99-preserve-frontend"
cat > /etc/sudoers.d/99-preserve-frontend <<'EOF'
Defaults env_keep += "DEBIAN_FRONTEND NEEDRESTART_MODE"
EOF
chmod 440 /etc/sudoers.d/99-preserve-frontend
visudo -cf /etc/sudoers.d/99-preserve-frontend >/dev/null \
    || die "sudoers file we just wrote is invalid — investigate /etc/sudoers.d/99-preserve-frontend"

# ── 5/10  Clean any prior half-installed Vast state ────────────────────
say "5/10  cleanup of any prior install state"
systemctl stop vastai vast_metrics vastai_bouncer 2>/dev/null
fuser -k /var/lib/docker 2>/dev/null
umount -l /var/lib/docker 2>/dev/null
losetup -D 2>/dev/null
sed -i '/docker/d' /etc/fstab
userdel -f vastai_kaalia 2>/dev/null
groupdel vastai_kaalia 2>/dev/null
rm -rf \
    /var/lib/vastai_kaalia \
    /var/lib/docker \
    /var/lib/docker-loop.xfs \
    /var/lib/docker-temporarily-renamed \
    /etc/systemd/system/vastai* \
    /etc/systemd/system/vast_metrics.service \
    /etc/systemd/system/vastai_bouncer.service \
    /etc/systemd/system/var-lib-docker.mount \
    /etc/systemd/system/multi-user.target.wants/vastai* \
    /etc/systemd/system/multi-user.target.wants/vast_metrics.service \
    /etc/docker \
    /var/spool/cron/crontabs/vastai_kaalia \
    /tmp/install \
    /root/vastai_install_logs.tar.gz
systemctl daemon-reload
echo "  state cleaned"

# Recover dpkg if a previous install was kill -9'd
if ! dpkg --audit 2>&1 | head -1 | grep -q '^$'; then
    echo "  dpkg in interrupted state — running dpkg --configure -a"
    DEBIAN_FRONTEND=noninteractive dpkg --configure -a >/dev/null 2>&1 || true
fi

# Fix the broken netplan that older MeowOS images shipped. The `all-other`
# block matches `driver: "*"` which catches Docker's veth interfaces;
# systemd-networkd then "manages" them as standalone DHCP clients and
# detaches them from docker0, killing container networking. Vast's
# `Test docker` step then fails with "no servers could be reached" /
# "registry-1.docker.io network unreachable", install aborts, services
# never get enabled. Idempotent — does nothing if `all-other` already gone.
if [[ -f /etc/netplan/01-mfarm.yaml ]] && grep -q 'all-other' /etc/netplan/01-mfarm.yaml; then
    echo "  patching /etc/netplan/01-mfarm.yaml to drop all-other wildcard"
    cp /etc/netplan/01-mfarm.yaml /etc/netplan/01-mfarm.yaml.preVast
    python3 - <<'PY'
import yaml
p = "/etc/netplan/01-mfarm.yaml"
with open(p) as f: cfg = yaml.safe_load(f)
ethers = cfg.get("network", {}).get("ethernets", {})
if "all-other" in ethers:
    del ethers["all-other"]
    with open(p, "w") as f: yaml.safe_dump(cfg, f, default_flow_style=False)
PY
    chmod 600 /etc/netplan/01-mfarm.yaml
    netplan apply 2>&1 | grep -v Permissions || true
    sleep 2
    # docker bridge needs a refresh to pick up the new networking
    systemctl restart docker 2>/dev/null || true
    sleep 3
fi

# ── 6/10  Pre-cache Docker packages ────────────────────────────────────
# Vast's installer fetches these from download.docker.com mid-install. That CDN
# has timed out on us repeatedly (174 kB/s, broken PPA mirror). Cache the .debs
# now while the network is good — apt will use the cached versions later.
say "6/10  pre-cache Docker / nvidia-docker .debs"
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -d --fix-missing \
    nvidia-docker2 docker-ce docker-ce-cli docker-buildx-plugin \
    docker-compose-plugin docker-ce-rootless-extras containerd.io \
    >/dev/null 2>&1 \
    || warn "some packages didn't pre-cache (may be repo not set up yet — install will set repos and retry)"

CACHED=$(ls /var/cache/apt/archives/ 2>/dev/null | grep -cE '^(docker|nvidia-)' || echo 0)
echo "  ${CACHED} docker/nvidia .debs in apt cache"

# ── 7/10  Fetch the install script with retries ────────────────────────
say "7/10  fetch Vast install script"
rm -f /tmp/install
for try in 1 2 3 4 5; do
    wget -q --tries=2 --timeout=15 https://console.vast.ai/install -O /tmp/install
    if [[ -s /tmp/install ]] && head -1 /tmp/install | grep -q 'python'; then
        echo "  fetched $(stat -c%s /tmp/install) bytes"
        break
    fi
    warn "wget attempt $try produced an empty/bad file, retrying in 5s..."
    rm -f /tmp/install
    sleep 5
done
[[ -s /tmp/install ]] || die "could not fetch a valid install script after 5 tries"

# ── 8/10  Launch install in screen (survives SSH drops) ────────────────
say "8/10  launch install in screen 'vastinstall' (logs to $LOG)"
> "$LOG"
screen -dmS vastinstall bash -c "
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
    python3 /tmp/install '$TOKEN' \
        --ports $PORT_START $PORT_END \
        --no-driver \
        --agree-to-nvidia-license 2>&1 | tee $LOG
    echo === EXIT \$? === >> $LOG
"
echo "  install running detached. you can attach with: screen -r vastinstall"

# ── 9/10  Watch progress (timeout 20 min) ──────────────────────────────
say "9/10  watching install (timeout 20 min)"
LAST=""
DEADLINE=$(( $(date +%s) + 1200 ))
while (( $(date +%s) < DEADLINE )); do
    sleep 8
    if grep -q '=== EXIT' "$LOG" 2>/dev/null; then break; fi
    NEW=$(grep -oE '=> [^[:cntrl:]]+' "$LOG" 2>/dev/null | tail -1)
    if [[ -n "$NEW" && "$NEW" != "$LAST" ]]; then
        echo "  $NEW"
        LAST="$NEW"
    fi
done

RECOVERY_OK=0
if grep -q 'Install failed' "$LOG"; then
    echo
    echo "INSTALL FAILED. Tail of log:"
    tail -20 "$LOG"

    # First-pass triage: detect token-already-consumed (401 from identify) so
    # the operator gets a clear, actionable message instead of having to crack
    # open /root/vastai_install_logs.tar.gz to spot the buried HTTPError trace.
    # Each Vast install token is single-use — calling
    # /api/v0/daemon/identify/ a second time always returns 401. We surface
    # this case explicitly because it's the #1 source of "vast won't install
    # on rig X" tickets after the first rig succeeds with a portal-fresh token.
    install_log=/root/vast_host_install.log
    if grep -qE '401 (Client Error|Unauthorized).*identify' "$install_log" 2>/dev/null \
       || grep -qE 'Unauthorized for url.*daemon/identify' "$install_log" 2>/dev/null; then
        echo
        echo "============================================================"
        echo " Vast install token has already been consumed (HTTP 401)."
        echo " Each token is single-use — get a fresh one from:"
        echo "   https://cloud.vast.ai/host/setup/"
        echo " then re-run with the new token."
        echo "============================================================"
        exit 4
    fi

    # Recovery for the common 'NVML test failure' mode. Symptoms: the python
    # installer made it past identify (machine_id is set) and installed Docker,
    # but its `test_nvml_error.sh` fails because `/etc/docker/daemon.json` was
    # written without a `runtimes.nvidia` entry — the test uses
    # `docker run --runtime=nvidia` which then errors with
    # "unknown or invalid runtime name: nvidia".
    #
    # Fix: register the runtime via nvidia-ctk (preserves Vast's existing
    # daemon.json keys), restart docker, re-run Vast's exact test, and if it
    # passes, start the services manually. The token was already consumed at
    # identify, so this avoids burning a fresh one.
    if [[ -f /var/lib/vastai_kaalia/machine_id ]] \
       && [[ -f /var/lib/vastai_kaalia/test_nvml_error.sh ]] \
       && grep -qE '(NVML|nvml|Test docker)' "$LOG"; then
        say "RECOVERY: NVML-test failure detected — configuring nvidia docker runtime"
        if command -v nvidia-ctk >/dev/null 2>&1; then
            nvidia-ctk runtime configure --runtime=docker --set-as-default=false 2>&1 | tail -3 \
                || warn "nvidia-ctk runtime configure failed"
            systemctl restart docker
            sleep 3
            test_out=$(bash /var/lib/vastai_kaalia/test_nvml_error.sh 2>&1 | tail -3)
            if echo "$test_out" | grep -q 'does not have the problem'; then
                echo "  NVML test now passes — starting services"
                systemctl daemon-reload
                systemctl enable --now vastai vast_metrics 2>&1 | tail -3
                sleep 5
                if systemctl is-active --quiet vastai; then
                    echo "  vastai service active — recovery succeeded, continuing to step 10"
                    RECOVERY_OK=1
                    # fall through to post-install hardening below
                else
                    echo "  vastai still inactive after recovery"
                    journalctl -u vastai --no-pager -n 10 2>&1 | tail -10
                    exit 2
                fi
            else
                echo "  NVML test still fails:"
                echo "$test_out"
                exit 2
            fi
        else
            echo "  nvidia-ctk not installed — can't recover automatically"
            exit 2
        fi
    else
        echo
        echo "Full diagnostic logs at: /root/vastai_install_logs.tar.gz"
        echo "If it died after identify, your token is burned — get a fresh one before retrying."
        exit 2
    fi
fi
# Skip the 'Daemon Running' check when our recovery path succeeded —
# /var/log/vast-install.log won't have that string because we side-stepped
# Vast's normal happy path. systemctl is-active is the actual success signal.
if [[ "$RECOVERY_OK" == "0" ]] && ! grep -q 'Daemon Running' "$LOG"; then
    echo "INSTALL TIMED OUT after 20 min without 'Daemon Running'. Last log lines:"
    tail -10 "$LOG"
    exit 3
fi

# ── 10/10  Post-install hardening ──────────────────────────────────────
say "10/10  post-install fixes"

# vast_metrics.service ships launch_metrics_pusher.sh without +x. Vast's
# auto-update re-extracts daemon.tar.gz periodically and the perm reverts —
# permanent fix is an ExecStartPre chmod in the unit.
if [[ -f /etc/systemd/system/vast_metrics.service ]] && \
   ! grep -q '^ExecStartPre=/bin/chmod' /etc/systemd/system/vast_metrics.service; then
    sed -i '/^\[Service\]/a ExecStartPre=/bin/chmod +x /var/lib/vastai_kaalia/latest/launch_metrics_pusher.sh /var/lib/vastai_kaalia/latest/machine_metrics_pusher.py' \
        /etc/systemd/system/vast_metrics.service
    systemctl daemon-reload
    systemctl restart vast_metrics
    echo "  patched vast_metrics.service with ExecStartPre chmod"
fi

# Vast's launch_kaalia.sh hardcodes `skip_bwtest=1` which prevents the daemon
# from running its bandwidth + speedtest cycle. Without those measurements,
# Vast's verification queue can never approve the machine for listing — it
# stays "unverified" forever and never earns. Drop a systemd override that
# strips the flag on every vastai start, so it survives Vast auto-updates
# (which would otherwise restore the flag in launch_kaalia.sh).
mkdir -p /etc/systemd/system/vastai.service.d
cat > /etc/systemd/system/vastai.service.d/no-skip-bwtest.conf <<'NSB'
[Service]
# Strip skip_bwtest=1 from Vast's launch script before each daemon start.
# Vast auto-update overwrites launch_kaalia.sh periodically; this guarantees
# the flag is gone every time the service comes up.
ExecStartPre=/bin/sed -i 's/ skip_bwtest=1//g' /var/lib/vastai_kaalia/latest/launch_kaalia.sh
NSB
systemctl daemon-reload
# Apply the edit immediately too (before first restart) and restart vastai
# so the daemon picks up no-skip-bwtest on this run.
sed -i 's/ skip_bwtest=1//g' /var/lib/vastai_kaalia/latest/launch_kaalia.sh 2>/dev/null || true
systemctl restart vastai 2>/dev/null || true
echo "  installed vastai drop-in to disable skip_bwtest (enables verification self-test)"

# Push fresh machine_info so the Vast portal updates within ~30s instead of
# waiting for the next cron-scheduled push (which can be ~1h away).
sudo -u vastai_kaalia bash -c 'cd /var/lib/vastai_kaalia && python3 send_mach_info.py' 2>&1 \
    | tail -1

# Restore mfarm-agent so xmrig (CPU mining per the rig's flight sheet) keeps
# running even during Vast rentals. CPU work doesn't compete with the
# renter's GPU workload, so we want it earning continuously. If the unit
# file is missing (legacy state from earlier install-vast.sh runs that
# rm'd it), recreate it from the canonical template.
if [[ ! -f /etc/systemd/system/mfarm-agent.service ]]; then
    cat > /etc/systemd/system/mfarm-agent.service <<'AGENTSVC'
[Unit]
Description=MeowFarm Mining Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/python3 /opt/mfarm/mfarm-agent.py
Restart=always
RestartSec=10
WatchdogSec=120
WorkingDirectory=/opt/mfarm
StandardOutput=append:/var/log/mfarm/agent.log
StandardError=append:/var/log/mfarm/agent.log
OOMScoreAdjust=-900
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
AGENTSVC
    echo "  restored mfarm-agent.service from template"
fi
# Deprioritize the agent + xmrig so a Vast renter's docker workload always
# preempts CPU mining. Children of the service (xmrig) inherit Nice from
# the parent process and share CPUWeight via cgroup. With these in place:
#
#   * Nice=19 — xmrig is "be polite to everyone." Renters at default Nice 0
#     get scheduled first; xmrig only fills idle cycles.
#   * CPUWeight=10 (default 100) — under contention, the renter gets ~10x
#     the CPU share. xmrig still earns whenever the renter isn't CPU-bound.
#
# Applied as a drop-in (not in the base mfarm-agent.service) so non-Vast
# rigs keep normal priority — there's nothing to compete with there, and
# Nice=19 would needlessly pessimize them.
mkdir -p /etc/systemd/system/mfarm-agent.service.d
cat > /etc/systemd/system/mfarm-agent.service.d/vast-deprioritize.conf <<'DEPRIO'
[Service]
Nice=19
CPUWeight=10
DEPRIO

systemctl unmask mfarm-agent 2>/dev/null  # idempotent: clears any prior install run's mask
systemctl daemon-reload
systemctl enable --now mfarm-agent 2>&1 | tail -2
echo "  mfarm-agent: $(systemctl is-active mfarm-agent) (CPU mining at Nice=19, CPUWeight=10)"

# ── Summary ────────────────────────────────────────────────────────────
say "DONE"
MACHINE_ID=$(cat /var/lib/vastai_kaalia/machine_id 2>/dev/null)
LOCAL_IP=$(hostname -I | awk '{print $1}')
PUBLIC_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null)

cat <<EOF

  status:      vastai $(systemctl is-active vastai), vast_metrics $(systemctl is-active vast_metrics)
  GPU:         ${GPU_NAME}
  internal:    ${LOCAL_IP}
  public:      ${PUBLIC_IP:-?}
  port range:  ${PORT_START}-${PORT_END}
  machine_id:  ${MACHINE_ID}

NEXT STEPS (manual, on the Vast host portal):
  1. Forward TCP+UDP ${PORT_START}-${PORT_END} on the router → ${LOCAL_IP}
     (skip if already done for this rig's IP)
  2. Click 'List' on https://cloud.vast.ai/host/machines/ for machine_id
     ${MACHINE_ID:0:16}...
EOF
