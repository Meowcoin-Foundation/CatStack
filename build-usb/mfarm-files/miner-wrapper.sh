#!/usr/bin/env bash
# MFarm Miner Wrapper - launches miner with logging and crash recovery
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
        exec "$BINARY" -a "$ALGO" -o "$POOL" -u "$WALLET.$WORKER" -p "$PASSWORD" \
            --http-host=0.0.0.0 --http-port="$API_PORT" $EXTRA_ARGS
        ;;
    *)
        echo "Unknown miner: $MINER"
        exit 1
        ;;
esac
