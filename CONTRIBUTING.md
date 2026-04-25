# Contributing to CatStack

## Repo layout

```
CatStack/
├── mfarm/              # Python package — CLI, web dashboard, agent
├── desktop/            # PyInstaller spec + launcher for the native app
├── build-usb/          # MeowOS image builder scripts
├── install.sh          # curl-based installer for Linux/Mac farm managers
└── VERSION             # bump this to cut a stable release
```

## Cutting a release

Change the number in `VERSION` and push to `main`. That's it — CI handles everything else.

```bash
echo "1.3.0" > VERSION
git add VERSION && git commit -m "Bump version to 1.3.0" && git push
```

What happens next:
1. `auto-tag.yml` sees the VERSION change, creates and pushes tag `v1.3.0`
2. `build-meowos.yml` and `build-app.yml` are dispatched in parallel
3. Both builds upload artifacts and manifest JSON files to storage
4. The download website picks up the new manifests automatically

## Dev builds

Every push to `develop` triggers a full build automatically — no version bump needed.
Builds are stamped with the short commit hash (e.g. `dev-abc1234`) and land in a
`dev/` prefix in storage, separate from stable releases.

```
stable:  manifest-meowos.json           meowos-v1.3.0.img.xz
dev:     dev/manifest-meowos.json       dev/meowos-dev-abc1234.img.xz
```

The manifests include a `"channel"` field (`"stable"` or `"dev"`) so the website
can tell them apart.

## What gets built

| Workflow | Output |
|----------|--------|
| `build-meowos.yml` | MeowOS disk image (`.img.xz`) — the mining rig OS |
| `build-app.yml` | CatStack desktop app for Windows + Linux (PyInstaller) |

## Build provenance

Every MeowOS image is signed via GitHub Sigstore. Anyone can verify a download:

```bash
gh attestation verify meowos-v1.3.0.img.xz --repo Meowcoin-Foundation/CatStack
```

## Building locally

**MeowOS image** — Linux or WSL2:
```bash
sudo MEOWOS_SRC=$(pwd) MEOWOS_OUTPUT=/tmp/meowos.img bash build-usb/wsl-build-image.sh
```

**Desktop app:**
```bash
pip install pyinstaller && pip install -e .
pyinstaller desktop/CatStack.spec --clean --noconfirm
# output: dist/CatStack/
```

**mfarm CLI (dev install):**
```bash
pip install -e ".[dev]"
mfarm --help
```
