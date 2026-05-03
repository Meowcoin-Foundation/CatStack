#!/bin/bash
# CatStack: `miner` — attach to the running miner's live output.
#
# Installed at /usr/local/bin/miner so it works in every shell (interactive,
# non-interactive `bash -c`, scripts), unlike a /etc/profile.d shell function
# which only loads in login shells.
#
# Usage:
#   miner              attach to / tail the running miner's log (default)
#   miner log          alias for the above
#   miner start        start the miner (via mfarm-agent or HiveOS)
#   miner stop         stop the miner
#   miner restart      restart the miner
#
# Detection order for "what's the running miner":
#   1. Screen session named miner / rigel-* (HiveOS pattern) → screen -r
#   2. Any running miner process (rigel, SRBMiner, ccminer, t-rex, xmrig,
#      lolMiner, miniZ, kerrigan, ethminer, kawpowminer); pick the one whose
#      log file has been written to most recently.
#   3. /var/log/mfarm/miner.log (mfarm-agent's stdout redirect for ccminer).
#   4. /hive/bin/miner pass-through (HiveOS).
#
# Log resolution per miner pid:
#   a. /proc/<pid>/fd/1 — if stdout is redirected to a regular file, that's it.
#   b. --log-file=PATH or --log-file PATH or -l PATH in cmdline.
#   c. Hardcoded fallback (ccminer → /var/log/mfarm/miner.log).

set -u

# ── helpers ────────────────────────────────────────────────────────────

# Names of miner binaries we recognise. We match against the comm field
# (program name, max 15 chars) with line anchors via `grep -E` — Ubuntu's
# mawk does NOT support `\b` word boundaries, so we use ` NAME$` anchored
# at end-of-line instead. ps `pid=,comm=` emits "<pid> <comm>".
_MINER_RE='^[[:space:]]*[0-9]+[[:space:]]+(rigel|SRBMiner-MULTI|SRBMiner|ccminer|t-rex|trex|xmrig|lolMiner|miniZ|kerrigan|ethminer|kawpowminer|nbminer|gminer|teamredminer)[[:space:]]*$'

list_miner_pids() {
    ps -e -o pid=,comm= | grep -E "$_MINER_RE" | awk '{print $1}'
}

log_for_pid() {
    local pid="$1"
    [ -d "/proc/$pid" ] || return 1

    # 1. stdout (fd 1) symlink
    local target
    target=$(readlink "/proc/$pid/fd/1" 2>/dev/null)
    if [ -n "$target" ] && [ -f "$target" ] && [ "$target" != /dev/null ]; then
        echo "$target"; return 0
    fi
    # Sometimes stdout is /dev/null but stderr (fd 2) goes to a file.
    target=$(readlink "/proc/$pid/fd/2" 2>/dev/null)
    if [ -n "$target" ] && [ -f "$target" ] && [ "$target" != /dev/null ]; then
        echo "$target"; return 0
    fi

    # 2. --log-file in cmdline (handles '--log-file=X' AND '--log-file X')
    local cmd
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
    local lf
    lf=$(echo "$cmd" | grep -oE -- '--log-file[= ][^ ]+' | head -1 | sed -E 's/--log-file[= ]//')
    if [ -n "$lf" ] && [ -f "$lf" ]; then
        echo "$lf"; return 0
    fi
    # short -l form (xmrig/some others)
    lf=$(echo "$cmd" | grep -oE -- '(^| )-l[= ][^ ]+' | head -1 | sed -E 's/^( )?-l[= ]//')
    if [ -n "$lf" ] && [ -f "$lf" ]; then
        echo "$lf"; return 0
    fi

    # 3. Per-miner heuristics for cases where stdout/stderr go to /dev/null
    #    and no --log-file flag is set.
    local comm
    comm=$(ps -p "$pid" -o comm= 2>/dev/null)
    case "$comm" in
        ccminer|cpuminer*)
            # mfarm-agent redirects ccminer's stdout to /var/log/mfarm/miner.log
            [ -f /var/log/mfarm/miner.log ] && { echo /var/log/mfarm/miner.log; return 0; }
            ;;
        rigel)
            # HiveOS layout
            [ -f /var/log/miner/rigel/rigel.log ] && { echo /var/log/miner/rigel/rigel.log; return 0; }
            ;;
    esac
    return 1
}

# Pick the miner whose log was most recently written. Echoes "<pid> <log>".
pick_active_miner() {
    local best_pid="" best_log="" best_mtime=0
    local pid log mtime
    for pid in $(list_miner_pids); do
        log=$(log_for_pid "$pid") || continue
        mtime=$(stat -c %Y "$log" 2>/dev/null) || continue
        if [ "$mtime" -gt "$best_mtime" ]; then
            best_mtime=$mtime; best_pid=$pid; best_log=$log
        fi
    done
    if [ -n "$best_pid" ]; then
        echo "$best_pid $best_log"
        return 0
    fi
    return 1
}

# ── subcommand: attach ─────────────────────────────────────────────────
cmd_attach() {
    # 1. HiveOS-style screen session (skip ones flagged Dead — those would
    # error with "No screen session found" or refuse to attach).
    local sn
    sn=$(screen -ls 2>/dev/null | awk '/[0-9]+\.(miner|rigel)/ && !/Dead/ {print $1; exit}')
    if [ -n "$sn" ]; then
        exec screen -r "$sn"
    fi

    # 2. Active miner process → its log
    local picked
    if picked=$(pick_active_miner); then
        local pid="${picked%% *}"
        local log="${picked#* }"
        local comm
        comm=$(ps -p "$pid" -o comm= 2>/dev/null)
        echo "miner: tailing $log (pid $pid, $comm)" >&2
        exec tail -n 50 -f "$log"
    fi

    # 3. Last-resort fallbacks (no live miner running)
    if [ -f /var/log/mfarm/miner.log ]; then
        echo "miner: no active miner process found; tailing last mfarm log" >&2
        exec tail -n 50 -f /var/log/mfarm/miner.log
    fi
    if [ -x /hive/bin/miner ]; then
        exec /hive/bin/miner
    fi
    echo "miner: no running miner detected and no log files found" >&2
    return 1
}

# ── subcommand: start/stop/restart ─────────────────────────────────────
cmd_control() {
    local sub="$1"
    if [ -x /hive/bin/miner ]; then
        exec /hive/bin/miner "$sub"
    fi
    if [ -d /var/run/mfarm ] || [ -f /etc/mfarm/config.json ]; then
        local cmd_file=/var/run/mfarm/command
        local sudo_pfx=""
        [ -w "$(dirname "$cmd_file")" ] || sudo_pfx="sudo"
        $sudo_pfx mkdir -p "$(dirname "$cmd_file")" 2>/dev/null
        echo "${sub}_miner" | $sudo_pfx tee "$cmd_file" >/dev/null
        echo "miner: queued '${sub}_miner' to mfarm-agent (watches $cmd_file)" >&2
        return 0
    fi
    echo "miner: no /hive/bin/miner and no mfarm-agent on this rig" >&2
    return 1
}

# ── dispatch ───────────────────────────────────────────────────────────
case "${1:-}" in
    ""|log)             cmd_attach ;;
    start|stop|restart) cmd_control "$1" ;;
    -h|--help|help)
        sed -n '2,12p' "$0" | sed 's/^# \?//'
        ;;
    *)
        # Unknown subcommand: defer to Hive's miner if present.
        if [ -x /hive/bin/miner ]; then
            exec /hive/bin/miner "$@"
        fi
        echo "miner: unknown subcommand '$1'" >&2
        echo "  supported: miner [log] | miner start | miner stop | miner restart" >&2
        exit 1
        ;;
esac
