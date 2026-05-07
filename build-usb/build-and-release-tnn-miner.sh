#!/usr/bin/env bash
# Operator helper: rebuild tnn-miner from source on rig-06 and publish a
# CatStack release that miner-downloader.sh fetches. Run from the console PC
# after upstream tnn-miner cuts a new tag.
#
# Usage:
#   ./build-and-release-tnn-miner.sh <upstream-tag>
# Example:
#   ./build-and-release-tnn-miner.sh v0.8.2
#
# WITH_OROCHI was added after the 0.4.6-r3 GitHub release; only repo tags
# v0.7.x / v0.8.x have it. Use one of those (no published GitHub release
# is needed — git clone --branch <tag> works against repo tags).
#
# What it does:
#   1. Triggers a clean build on rig-06 (Orochi target, CPM Boost, clang-20)
#   2. Pulls the resulting binary + bundles libnvrtc/libnvrtc-builtins
#   3. Tarballs as tnn-miner-orochi-linux-x86_64.tar.gz
#   4. Creates GitHub release `tnn-miner-orochi-<tag>` on
#      Meowcoin-Foundation/CatStack with the tarball attached
#   5. Reminds operator to bump miner-downloader.sh URL + VERSION

set -euo pipefail

TAG="${1:?Usage: $0 <upstream-tag>}"
RIG_HOST="miner@192.168.69.11"
BUILD_DIR="/home/miner/tnn-build"
RELEASE_TAG="tnn-miner-orochi-${TAG}"
TARBALL="tnn-miner-orochi-linux-x86_64.tar.gz"
LOCAL_STAGE="/tmp/tnn-stage-${TAG}"

echo "=== Phase 1: build on rig-06 (will take ~30-60 min) ==="
ssh "$RIG_HOST" bash -s -- "$TAG" <<'REMOTE'
set -euo pipefail
TAG="$1"
cd /home/miner/tnn-build
rm -rf tnn-miner
git clone --depth 1 -b "$TAG" https://github.com/Tritonn204/tnn-miner.git
cd tnn-miner
mkdir -p build && cd build
cmake -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DUSE_CPM_BOOST=ON \
  -DWITH_OROCHI=ON \
  -DCMAKE_C_COMPILER=clang-20 \
  -DCMAKE_CXX_COMPILER=clang++-20 \
  -DTNN_VERSION="$TAG" ..
nice -n 19 ninja -j 4
file ./tnn-miner | head -1
ls -lh ./tnn-miner
REMOTE

echo "=== Phase 2: stage binary + libnvrtc on console ==="
rm -rf "$LOCAL_STAGE" "$LOCAL_STAGE.tar.gz"
mkdir -p "$LOCAL_STAGE/tnn-miner-orochi/lib"

# Pull the binary
scp "$RIG_HOST:$BUILD_DIR/tnn-miner/build/tnn-miner" "$LOCAL_STAGE/tnn-miner-orochi/tnn-miner"
chmod +x "$LOCAL_STAGE/tnn-miner-orochi/tnn-miner"

# Bundle libnvrtc + builtins from rig-06's CUDA install (Orochi dlopens these
# at runtime; production rigs only have libcudart12, not nvrtc).
ssh "$RIG_HOST" 'cd /usr/local/cuda/targets/x86_64-linux/lib && tar czf - libnvrtc.so* libnvrtc-builtins.so*' \
    | tar xzf - -C "$LOCAL_STAGE/tnn-miner-orochi/lib"
ls -lh "$LOCAL_STAGE/tnn-miner-orochi/lib/"

echo "=== Phase 3: tarball + release ==="
tar czf "$LOCAL_STAGE.tar.gz" -C "$LOCAL_STAGE" tnn-miner-orochi
ls -lh "$LOCAL_STAGE.tar.gz"

# Smoke-test version banner
"$LOCAL_STAGE/tnn-miner-orochi/tnn-miner" --help 2>&1 | head -3 || true

# Create release
gh release create "$RELEASE_TAG" \
    --repo Meowcoin-Foundation/CatStack \
    --title "tnn-miner Orochi build $TAG (NVIDIA Linux)" \
    --notes "tnn-miner $TAG built with WITH_OROCHI=ON for Meowcoin-Foundation/CatStack rigs.

Bundles libnvrtc/libnvrtc-builtins so MeowOS rigs (libcudart12 only) can run it without installing CUDA toolkit.

Source: https://github.com/Tritonn204/tnn-miner/tree/$TAG" \
    "$LOCAL_STAGE.tar.gz#$TARBALL"

echo "=== Phase 4: next steps ==="
cat <<NEXT
Release URL: https://github.com/Meowcoin-Foundation/CatStack/releases/tag/$RELEASE_TAG
Asset URL:   https://github.com/Meowcoin-Foundation/CatStack/releases/download/$RELEASE_TAG/$TARBALL

Now:
  1. Update mfarm/worker/miner-downloader.sh — point the tnn-miner entry at
     the asset URL above.
  2. Bump VERSION ($(cat /mnt/c/Source/mfarm/VERSION 2>/dev/null || cat C:/Source/mfarm/VERSION) -> next patch).
  3. git commit, git push.
  4. Restart console:  systemctl restart catstack
  5. Trigger update on rigs:
       curl -X POST http://localhost:8888/api/rigs/all/update-miners
NEXT
