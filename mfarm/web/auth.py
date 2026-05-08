"""Bearer-token auth for the dashboard (mobile / remote access).

Localhost is exempt so the desktop launcher (which loads
http://127.0.0.1:8888 in a Chromium window) keeps working without any
token plumbing. Agent endpoints (/api/agent/*) are also exempt — they
have their own per-rig token model in agent_api.py.

The token is read from $MFARM_API_TOKEN if set; otherwise from
~/.config/mfarm/api_token (auto-generated on first import). Print it
once at startup so the operator can paste it into the Android app.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocket

log = logging.getLogger(__name__)

_TOKEN_PATH = Path.home() / ".config" / "mfarm" / "api_token"


def _load_or_create_token() -> str:
    env = os.environ.get("MFARM_API_TOKEN", "").strip()
    if env:
        return env
    try:
        if _TOKEN_PATH.is_file():
            t = _TOKEN_PATH.read_text(encoding="utf-8").strip()
            if t:
                return t
    except Exception as e:
        log.warning("Could not read %s: %s", _TOKEN_PATH, e)

    token = secrets.token_urlsafe(32)
    try:
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _TOKEN_PATH.write_text(token, encoding="utf-8")
        try:
            os.chmod(_TOKEN_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        log.warning("Could not persist token to %s: %s", _TOKEN_PATH, e)
    return token


API_TOKEN = _load_or_create_token()


def _is_localhost(request: Request | WebSocket) -> bool:
    client = request.client
    if not client:
        return False
    return client.host in ("127.0.0.1", "::1", "localhost")


def _requires_auth(path: str) -> bool:
    if path.startswith("/api/agent/"):
        return False
    return path.startswith("/api/")


def _extract_bearer(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


async def auth_middleware(request: Request, call_next):
    if not _requires_auth(request.url.path) or _is_localhost(request):
        return await call_next(request)

    presented = _extract_bearer(request.headers.get("authorization"))
    if presented and secrets.compare_digest(presented, API_TOKEN):
        return await call_next(request)

    return JSONResponse({"error": "unauthorized"}, status_code=401)


def authorize_websocket(ws: WebSocket) -> bool:
    """Return True if the WS handshake is allowed.

    Localhost is allowed. Otherwise the client must present the token via
    Authorization: Bearer header OR a ?token= query parameter (the latter
    is for clients that can't easily set headers on the WS upgrade).
    """
    if _is_localhost(ws):
        return True
    presented = _extract_bearer(ws.headers.get("authorization"))
    if not presented:
        presented = ws.query_params.get("token")
    if presented and secrets.compare_digest(presented, API_TOKEN):
        return True
    return False
