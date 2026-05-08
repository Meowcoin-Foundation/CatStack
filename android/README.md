# CatStack Android app

Companion app for the CatStack dashboard. Lets you start/stop miners, switch
flight sheets, and apply OC profiles from your phone over Tailscale.

The app is read-only on networking — there's no remote-access magic on the
phone side. The recipe is: dashboard binds to 0.0.0.0, Tailscale gives both
the dashboard host and the phone a 100.x.y.z address, the phone hits the
dashboard at that address with a bearer token.

## Server-side: token auth

The FastAPI server now requires a bearer token for any non-localhost request
(`mfarm/web/auth.py`). On first startup `serve.py` prints the token:

```
============================================================
MFARM API TOKEN: <random url-safe string>
  (stored at ~/.config/mfarm/api_token)
  Localhost requests skip auth — desktop UI unaffected.
  Remote clients (Android app, etc.) must send this token.
============================================================
```

Persisted at `~/.config/mfarm/api_token` (mode 0600). To override, export
`MFARM_API_TOKEN=<value>` before launching `serve.py`. To rotate, delete the
file and restart.

The desktop launcher (`catstack.pyw` / `launcher_gui.py`) binds to
`127.0.0.1:8888`, so it skips auth entirely. Agent endpoints under
`/api/agent/*` are also exempt — they have their own per-rig token.

## Network: Tailscale

Install Tailscale on the dashboard host (Linux example):

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Install Tailscale on your Android phone (Play Store), sign into the same
account, and it will join the same tailnet automatically.

Find the dashboard host's tailnet IP with `tailscale ip -4` — that's what
goes in the app's "Server URL" field, e.g. `http://100.64.0.1:8888`.

Tailscale traffic is end-to-end encrypted, so plain HTTP is fine. The app's
`network_security_config.xml` allows cleartext for that reason.

## Building the APK

You need Android Studio (Hedgehog or newer) and JDK 17.

1. Open `android/` as a project in Android Studio. It will prompt to download
   the Gradle wrapper jar — accept.
2. **Build → Build Bundle(s) / APK(s) → Build APK(s)**.
3. The APK lands at `android/app/build/outputs/apk/debug/app-debug.apk`.

CLI alternative (if you have a system-installed Gradle 8.9+):

```sh
cd android
gradle wrapper --gradle-version 8.9      # one-time, generates gradlew
./gradlew assembleDebug
```

Side-load the APK to your phone via `adb install` or by transferring the
file and opening it (allow installs from this source in system settings).

## First-run flow

1. Launch the app — Settings opens because nothing is configured.
2. Enter the server URL (`http://<tailnet-ip>:8888`) and the token from
   `serve.py`'s startup banner.
3. Save. The rig list connects to the WebSocket and starts streaming.

## Capabilities

- Live rig list with hashrate / power / max GPU temp / status dot
  (green = mining, amber = online idle, red = offline)
- Tap rig → action sheet:
  - Start / Stop / Restart miner
  - Apply flight sheet (picks from server's list)
  - Apply OC profile (picks from server's list)

Everything else (rig CRUD, exec, OC editing, charts) stays on the desktop.
