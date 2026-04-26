# MeowFarm: `miner` shell function — make the running miner visible and control it.
#
# Usage:
#   miner              attach/show running miner output
#   miner start        start the miner
#   miner stop         stop the miner
#   miner restart      restart the miner
#   miner log          alias for `miner` (no args)
#   miner <other>      passes through to Hive's /hive/bin/miner if present
#
# Hive rigs:  uses /hive/bin/miner for control commands and attaches to the
#             `miner`/`rigel-*` screen for the bare invocation.
# mfarm rigs: writes start_miner/stop_miner/restart_miner to
#             /var/run/mfarm/command (the agent watches that file) and
#             tails /var/log/mfarm/miner.log for the bare invocation.

miner() {
    local sub="${1:-}"

    case "$sub" in
        ""|log)
            # Bare invocation: show running miner output.
            local sn
            sn=$(screen -ls 2>/dev/null | awk '/[0-9]+\.(miner|rigel)/{print $1; exit}')
            if [ -n "$sn" ]; then
                screen -r "$sn"
                return $?
            fi
            if [ -x /hive/bin/miner ]; then
                /hive/bin/miner
                return $?
            fi
            if [ -f /var/log/mfarm/miner.log ]; then
                echo "Tailing /var/log/mfarm/miner.log (Ctrl+C to exit)..." >&2
                exec tail -n 50 -f /var/log/mfarm/miner.log
            fi
            echo "miner: no miner screen, no /hive/bin/miner, no /var/log/mfarm/miner.log found" >&2
            return 1
            ;;
        start|stop|restart)
            # Hive: delegate to /hive/bin/miner so its watchdog state stays consistent.
            if [ -x /hive/bin/miner ]; then
                /hive/bin/miner "$@"
                return $?
            fi
            # mfarm: write the command to /var/run/mfarm/command. The agent
            # picks it up within ~2 seconds.
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
            ;;
        *)
            # Unknown subcommand: pass through to Hive's miner if present.
            if [ -x /hive/bin/miner ]; then
                /hive/bin/miner "$@"
                return $?
            fi
            echo "miner: subcommand '$sub' not supported on this rig" >&2
            echo "       supported: miner [log] | miner start | miner stop | miner restart" >&2
            return 1
            ;;
    esac
}
