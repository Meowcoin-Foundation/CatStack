"""Launcher for MeowFarm web dashboard - avoids CWD import conflict."""
import sys
import os

# Ensure we don't import from CWD
if os.getcwd() in sys.path:
    sys.path.remove(os.getcwd())

from mfarm.web.app import run_server
run_server(host="0.0.0.0", port=8888)
