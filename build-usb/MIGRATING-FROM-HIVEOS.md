# Migrating a HiveOS rig to MeowOS / CatStack

In-place flash via SSH + `dd`. The HiveOS root disk is overwritten with `meowos.img`,
the rig reboots into MeowOS first-boot, then CatStack auto-registers it and applies
a flight sheet. ~8 minutes per rig on GbE in the happy path.

The script is `build-usb/_migrate-hive-to-catstack.py`. This doc covers the wrapper:
prerequisites the script doesn't check, the per-rig Hive-Shell prep step, expected
timing, and how to recover from the failure modes we've actually hit.

## Prerequisites

| What | Why |
|---|---|
| CatStack web API at `http://192.168.68.78:8888` | Script POSTs to register rig + apply flight sheet |
| `C:\Source\meowos.img` (raw, ~10 GB) | What gets dd'd onto the rig — built via `wsl-build-image.sh` or downloaded from `cdn.catstack.sh/meowos-latest.img.xz` |
| UniFi controller at `192.168.68.1` reachable with creds in script | Used to MAC-pin the post-reboot IP and to find the rig after DHCP refreshes |
| Console PC can SSH directly to rig on `:22` | The dd-relay needs raw TCP from console → rig:9999 |
| `paramiko` + `requests` installed | `pip install paramiko requests` |

## Per-rig prep (do this BEFORE running the script)

HiveOS rigs in our fleet have **two issues** the migration script can't fix from outside:

1. **`sshd` only listens on `127.0.0.1:22`** — the script's SSH connect from the
   console PC gets `Connection refused` even though Hive Shell works (Hive Shell
   uses a cloud-relay TTY, not direct SSH).
2. **`user`'s password is sometimes not `1`** — the script auths as `user/1` (HiveOS
   default), but on some rigs it's been changed and you'll get
   `AuthenticationException`.

Fix both via Hive Shell (https://shell.hiveos.farm/?<token> from the rig in HiveOS web UI):

```bash
sed -i 's/^ListenAddress 127.0.0.1/#ListenAddress 127.0.0.1/' /etc/ssh/sshd_config \
  && systemctl restart ssh \
  && sleep 1 \
  && ss -lnt 'sport = :22' \
  && echo 'user:1' | chpasswd \
  && echo OK_FIXED
```

Expected output: `LISTEN ... 0.0.0.0:22` and `OK_FIXED`. If you see those, the
script will work. The rig is wiped in the next step anyway — these edits are throwaway.

## Run it

```bash
cd C:\Source\mfarm\build-usb
py -u _migrate-hive-to-catstack.py <rig_name> <rig_ip> [<flight_sheet>]
```

Flight-sheet default is `XTM_Working` (RandomX → Kryptex via xmrig). Other options
the rigs run: `LPEPE`, `Xelis-kryptex`, `kerrigan-equi192`, `kerrigan-lolminer`.

`<rig_name>` matters: when it matches `HiveMiniNN`, the script pre-pins
`192.168.70.NN` in UniFi DHCP so the rebooted MeowOS lands on a predictable IP.
Other names skip the pin (DHCP-assigned).

Recommended invocation — log to file, run in background:

```bash
LOG=C:\Users\benef\migration-logs\<RigName>-$(date +%Y%m%d-%H%M%S).log
py -u _migrate-hive-to-catstack.py HiveMini04 192.168.71.36 XTM_Working > "$LOG" 2>&1 &
```

## Timeline (typical, GbE, NVMe rig)

| Phase | Duration | What happens |
|---|---|---|
| Quiesce + nc bind | ~10s | `pkill miner`, launch detached `nc + dd` relay |
| dd push (10 GB) | ~90s @ 110 MB/s | Console streams `meowos.img` over TCP to rig:9999 |
| Reboot drop | ~30s | sysrq triggers reboot, SSH disappears |
| MeowOS first-boot | 3–5 min | sgdisk grow + resize2fs, sensors-detect, fresh DHCP, second reboot |
| Register + push-key + apply | ~30s | CatStack API calls |
| Verify hashing | up to 2 min | xmrig ramps |
| **Total** | **~8 min** | |

## Observability

Each script step prints a banner like `step 3:`, `step 4:`, `rig dropped`, `rig back as MeowOS`,
`registered`, `push-key`, `applied`, then `OK <RigName>: ...` and `DONE`.

Light filter for a `tail -F` monitor:

```bash
tail -F "$LOG" | grep -E "step [0-9]|rig MAC|unifi:|nc listening|rig dropped|rig back|registered|push-key|applied|OK |WARN|FAILED|Traceback|RuntimeError|DONE|GB \(100"
```

## Verify post-migration

Script's built-in verify exits OK on `accepted > 0` OR `hashrate > 100 H/s`, so
"OK ... 0.00 kH/s" can mean shares already landed but the cached hashrate snapshot
was zero. Confirm with the live API:

```bash
curl -s http://localhost:8888/api/rigs/<RigName>/stats | py -m json.tool
```

Look for `miner.running=true`, non-zero `hashrate`, growing `accepted`, zero `rejected`.
RandomX/xmrig takes 30–60s to allocate hugepages and reach steady-state hashrate.

## Failure modes (real, from the fleet rollout)

| Symptom | Cause | Fix |
|---|---|---|
| `NoValidConnectionsError: Unable to connect to port 22` | sshd bound to loopback | Run the prep snippet above via Hive Shell |
| `AuthenticationException: Authentication failed` | `user` password isn't `1` | `echo 'user:1' \| chpasswd` via Hive Shell, retry |
| `nc never started listening on :9999` | Stale relay state from prior failed attempt | Script's quiesce step now `pkill`s the prior `nc`+`dd` and `rm`s the FIFO; rerun |
| Push hangs / `send failed at X.X GB` | sshd died mid-flash on the rig (RAM pressure) | The detached `nc → FIFO → dd` design fixes the original sshd-death bug; if it still dies, image may need recovery via USB boot |
| `MeowOS didn't come back within 30 min` | First-boot crashed or DHCP didn't lease | Check UniFi for the rig's MAC; if missing, rig needs physical recovery (HDMI/keyboard) |

## What changes per rig

- **IP**: HiveOS DHCP IP → `192.168.70.NN` (UniFi pin) for `HiveMiniNN`-named rigs.
  Other names get a fresh DHCP lease anywhere in the `192.168.68.0/22` pool.
- **Hostname**: `HiveMiniNN` → `mfarm-rig-XXXX` (last 4 of MAC). The CatStack rig
  record keeps the original name; only the OS hostname changes.
- **Auth**: SSH key-only (CatStack pushes its `~/.ssh/id_ed25519.pub` to root). The
  HiveOS `user/1` account is gone with the OS image.
- **Mining**: Whatever flight sheet you applied. xmrig RandomX uses CPU only, so
  GPU rigs with `XTM_Working` will mine on CPU and leave GPUs idle — that's
  intentional for some rigs but worth confirming for any new ones.

## Rolling out the fleet

For batch migrations, prep multiple rigs' SSH ahead of time, then dd them serially —
the dd push is the bandwidth-bound phase (~90s) and the 4-min first-boot doesn't
touch the console PC, so prep N+1 while dd N is running.

Already migrated (as of 2026-05-03 session): HiveMini04, 05, 06, 07, 09, 18, 20,
22, 23, 29, 33, 36, 42.
