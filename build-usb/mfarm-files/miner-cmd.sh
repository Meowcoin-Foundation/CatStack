#!/bin/bash
# 'miner' — live miner summary + tailed log (MeowOS)
# Replaces HiveOS's miner command. Hits xmrig HTTP API for a one-glance
# header (algo, hashrate, shares, pool, uptime), then follows /var/log/mfarm/miner.log.
LOG=/var/log/mfarm/miner.log

xmrig_pid=$(pgrep -x xmrig 2>/dev/null | head -1)
if [ -n "$xmrig_pid" ]; then
    cmd=$(tr '\0' ' ' < /proc/$xmrig_pid/cmdline 2>/dev/null)
    port=$(echo "$cmd" | grep -oE -- '--http-port=[0-9]+' | head -1 | cut -d= -f2)
    token=$(echo "$cmd" | grep -oE -- '--http-access-token=\S+' | head -1 | cut -d= -f2)
    : ${port:=44445}; : ${token:=meowfarm}

    curl -fsS -m 2 -H "Authorization: Bearer $token" \
         "http://127.0.0.1:$port/2/summary" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("\033[33mxmrig running but HTTP API unreachable\033[0m")
    sys.exit(0)
hr = d.get("hashrate", {}).get("total", [0, 0, 0])
res = d.get("results", {})
conn = d.get("connection", {})
acc = res.get("shares_good", 0)
tot = res.get("shares_total", 0)
rej = tot - acc
cpu = d.get("cpu", {})
ut = int(conn.get("uptime", 0))
hms = "%dh%dm" % (ut // 3600, (ut % 3600) // 60)
threads = cpu.get("threads", 0)
cores = cpu.get("cores", 0)
brand = cpu.get("brand", "")
version = d.get("version", "?")
algo = d.get("algo", "?")
pool = conn.get("pool", "?")
diff = conn.get("diff", "?")
ping = conn.get("ping", "?")
def f(x):
    return ("%8.1f" % x) if x else "     n/a"
print("\033[1;36m═══ xmrig %s │ %s │ %dC/%dT │ up %s ═══\033[0m" % (version, algo, cores, threads, hms))
print("  cpu       %s" % brand)
print("  pool      \033[36m%s\033[0m" % pool)
print("  hashrate  \033[32m%s H/s (10s)  %s H/s (60s)  %s H/s (15m)\033[0m" % (f(hr[0]), f(hr[1]), f(hr[2])))
print("  shares    \033[32m%d accepted\033[0m / \033[31m%d rejected\033[0m / %d total" % (acc, rej, tot))
print("  diff      %s   ping %sms" % (diff, ping))
print()
'
elif pgrep -lf 'ccminer|rigel|SRBMiner|srbminer' >/dev/null 2>&1; then
    proc=$(pgrep -lf 'ccminer|rigel|SRBMiner|srbminer' | head -1)
    echo -e "\033[33m$proc (no HTTP summary support yet — log only)\033[0m\n"
else
    echo -e "\033[31mno miner process found\033[0m"
    echo "  mfarm-agent: $(systemctl is-active mfarm-agent 2>/dev/null)"
    echo
fi

if [ ! -f "$LOG" ]; then
    echo -e "\033[31mlog $LOG not found\033[0m"
    exit 1
fi

# Interactive shell (real ssh / tmux) → follow the log live. Non-interactive
# (dashboard web-SSH modal, scripted ssh-without-PTY) → just dump the last 30
# lines and exit, otherwise the caller hangs forever buffering -F output.
if [ -t 1 ]; then
    echo -e "\033[1m─── tailing $LOG (Ctrl-C to stop) ───\033[0m"
    exec tail -n 30 -f "$LOG"
else
    echo -e "\033[1m─── last 30 lines of $LOG ───\033[0m"
    tail -n 30 "$LOG"
fi
