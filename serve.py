"""Launcher for MeowFarm web dashboard - avoids CWD import conflict."""
import sys
import os

# Ensure we don't import from CWD
if os.getcwd() in sys.path:
    sys.path.remove(os.getcwd())

from mfarm.web.app import run_server
from mfarm.web.auth import API_TOKEN, _TOKEN_PATH

print("=" * 60)
print(f"MFARM API TOKEN: {API_TOKEN}")
print(f"  (stored at {_TOKEN_PATH})")
print("  Localhost requests skip auth — desktop UI unaffected.")
print("  Remote clients (Android app, etc.) must send this token.")
print("=" * 60, flush=True)

run_server(host="0.0.0.0", port=8888)
