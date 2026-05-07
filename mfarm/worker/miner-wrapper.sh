#!/usr/bin/env bash
# CatStack Miner Wrapper - launches miner with logging and crash recovery
# This is called by the agent but can also be used standalone for testing.
#
# Usage: miner-wrapper.sh <config-path>
# Config is read from /etc/mfarm/config.json by default.

set -uo pipefail

CONFIG="${1:-/etc/mfarm/config.json}"
LOG_DIR="/var/log/mfarm"
RUN_DIR="/var/run/mfarm"

mkdir -p "$LOG_DIR" "$RUN_DIR"

if [[ ! -f "$CONFIG" ]]; then
    echo "Config not found: $CONFIG"
    exit 1
fi

# Extract fields from config using python (guaranteed on the rig)
eval "$(python3 -c "
import json, sys
c = json.load(open('$CONFIG'))
fs = c.get('flight_sheet') or {}
print(f'MINER={fs.get(\"miner\", \"\")}')
print(f'ALGO={fs.get(\"algo\", \"\")}')
print(f'POOL={fs.get(\"pool_url\", \"\")}')
print(f'WALLET={fs.get(\"wallet\", \"\")}')
print(f'WORKER={fs.get(\"worker\", \"\")}')
print(f'PASSWORD={fs.get(\"password\", \"x\")}')
print(f'EXTRA_ARGS={fs.get(\"extra_args\", \"\")}')
print(f'IS_SOLO={1 if fs.get(\"is_solo\") else 0}')
print(f'RPC_USER={fs.get(\"solo_rpc_user\", \"\")}')
print(f'RPC_PASS={fs.get(\"solo_rpc_pass\", \"\")}')
print(f'COINBASE={fs.get(\"coinbase_addr\", \"\")}')

paths = c.get('miner_paths', {})
miner = fs.get('miner', '')
print(f'BINARY={paths.get(miner, miner)}')

ports = c.get('api_ports', {})
print(f'API_PORT={ports.get(miner, 4068)}')
")"

if [[ -z "$MINER" ]]; then
    echo "No miner configured in flight sheet"
    exit 1
fi

[[ -z "$WORKER" ]] && WORKER="$(hostname)"

echo "$(date) Starting $MINER ($ALGO) -> $POOL"
echo "  Wallet: $WALLET.$WORKER"
echo "  API Port: $API_PORT"
echo "  Extra: $EXTRA_ARGS"

exec >> "$LOG_DIR/miner.log" 2>&1

case "$MINER" in
    ccminer|cpuminer-opt|cpuminer)
        if [[ "$IS_SOLO" == "1" ]]; then
            exec "$BINARY" -a "$ALGO" -o "$POOL" -u "$RPC_USER" -p "$RPC_PASS" \
                --no-stratum --coinbase-addr="$COINBASE" --no-longpoll \
                -b "0.0.0.0:$API_PORT" --no-color $EXTRA_ARGS
        else
            exec "$BINARY" -a "$ALGO" -o "$POOL" -u "$WALLET.$WORKER" -p "$PASSWORD" \
                -b "0.0.0.0:$API_PORT" --no-color $EXTRA_ARGS
        fi
        ;;
    trex|t-rex)
        exec "$BINARY" -a "$ALGO" -o "$POOL" -u "$WALLET.$WORKER" -p "$PASSWORD" \
            --api-bind-http="0.0.0.0:$API_PORT" $EXTRA_ARGS
        ;;
    lolminer)
        exec "$BINARY" --algo "$ALGO" --pool "$POOL" --user "$WALLET.$WORKER" \
            --pass "$PASSWORD" --apiport="$API_PORT" $EXTRA_ARGS
        ;;
    xmrig)
        # --log-file + --http-access-token match the agent's direct-launch path
        # so CPU-slot XMRig behaves identically to GPU-slot XMRig for logging
        # and API access. Without these, miner.log stays empty and the
        # dashboard gets 401s querying /1/summary.
        exec "$BINARY" -a "$ALGO" -o "$POOL" -u "$WALLET.$WORKER" -p "$PASSWORD" \
            --http-host=0.0.0.0 --http-port="$API_PORT" \
            --http-access-token=meowfarm --http-no-restricted \
            --no-color --log-file="$LOG_DIR/cpu-miner.log" $EXTRA_ARGS
        ;;
    srbminer)
        exec "$BINARY" --algorithm "$ALGO" --pool "$POOL" --wallet "$WALLET" \
            --worker "$WORKER" --password "$PASSWORD" \
            --api-enable --api-port "$API_PORT" $EXTRA_ARGS
        ;;
    miniz)
        # Worker name goes in the stratum username as WALLET.WORKER — miniZ
        # does NOT parse it out of --url, the dot-delimited form IS the
        # stratum user. Without this every rig shows up as one anon worker.
        exec "$BINARY" --algo "$ALGO" --url "$WALLET.$WORKER@$POOL" \
            --telemetry="$API_PORT" --pers auto $EXTRA_ARGS
        ;;
    kerrigan)
        # Custom Equihash192,7 miner — launcher script spawns one mine.py +
        # kerrigan_v4 daemon per GPU. Pool is "host:port"; split for the script.
        HOST="${POOL%%:*}"
        PORT="${POOL##*:}"
        [[ "$PORT" == "$HOST" ]] && PORT=3202  # no ":port" in pool string
        if [[ "$BINARY" != *multi_gpu.sh ]]; then
            BINARY="$BINARY/multi_gpu.sh"
        fi
        exec "$BINARY" "$WALLET" "$WORKER" "$HOST" "$PORT"
        ;;
    tnn-miner|tnn)
        # tnn-miner uses --<coin-symbol> instead of -a, and splits the daemon
        # URL from the stratum port. The flight sheet's `algo` field carries
        # the tnn coin symbol (e.g. "lpepe", "xel-v3", "spr"); pass with `--`
        # prefix unless the user already prefixed it. Broadcast HTTP API is
        # fixed at compile time (TNN_BROADCAST_PORT=8989).
        case "$ALGO" in
            --*) COIN_FLAG="$ALGO" ;;
            *)   COIN_FLAG="--$ALGO" ;;
        esac
        # Split "scheme://host:port" or "host:port" into daemon + port
        if [[ "$POOL" =~ ^((stratum\+(tcp|ssl)://)?)([^:]+):([0-9]+)$ ]]; then
            DAEMON="${BASH_REMATCH[1]}${BASH_REMATCH[4]}"
            STRATUM_PORT="${BASH_REMATCH[5]}"
        else
            DAEMON="$POOL"
            STRATUM_PORT=""
        fi
        EXTRA_PORT_ARGS=()
        [[ -n "$STRATUM_PORT" ]] && EXTRA_PORT_ARGS=(--port "$STRATUM_PORT")
        exec "$BINARY" "$COIN_FLAG" \
            --daemon-address "$DAEMON" \
            --wallet "$WALLET.$WORKER" \
            --password "$PASSWORD" \
            --broadcast \
            "${EXTRA_PORT_ARGS[@]}" $EXTRA_ARGS
        ;;
    *)
        echo "Unknown miner: $MINER"
        exit 1
        ;;
esac
