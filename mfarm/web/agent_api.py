"""Rig-agent push/poll transport endpoints.

Replaces the dashboard's SSH-cat-stats.json pattern with an outbound-from-rig
HTTP push. Three endpoints, all bearer-auth'd by the rig's `agent_token`:

  POST /api/agent/stats   - rig pushes its stats.json equivalent
  GET  /api/agent/poll    - rig long-polls for queued commands (~25s)
  POST /api/agent/result  - rig posts a command's stdout/stderr/rc back

Dashboard endpoints enqueue commands via `agent_state.enqueue()` and read
the latest stats from `agent_state.get(rig_id).last_stats`. The legacy SSH
pool stays in place during rollout — see web/api.py:get_rig_stats for the
cache-first / SSH-fallback pattern.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request

from mfarm.db.connection import get_db
from mfarm.db.models import Rig
from mfarm.web import agent_state

log = logging.getLogger(__name__)

router = APIRouter()

# Long-poll timeout. Agent's HTTP read timeout must be > this; we use 25s
# server-side and 30s on the agent (urlopen timeout) so a clean empty
# response always wins the race vs. the agent giving up.
POLL_SECS = 25


def _auth(authorization: str | None) -> Rig:
    """Resolve `Authorization: Bearer <token>` to a Rig or raise 401.

    The token is opaque (operator-issued, stored in `rigs.agent_token`).
    Every endpoint in this module starts with this check — the rig is the
    untrusted side of the connection."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    rig = Rig.get_by_token(get_db(), authorization[7:])
    if rig is None:
        raise HTTPException(401, "unknown token")
    return rig


@router.post("/stats")
async def push_stats(req: Request, authorization: str | None = Header(None)):
    """Receive a stats push from the rig agent.

    Body is the same JSON shape the agent currently writes to
    /var/run/mfarm/stats.json — we don't validate it here so the agent can
    evolve the shape without a server-side schema bump. Last-write-wins;
    no history is kept (rig_snapshots persistence still happens in the
    dashboard's poll loop, separately).
    """
    rig = _auth(authorization)
    body = await req.json()
    s = agent_state.get(rig.id)
    s.last_stats = body
    s.last_seen = time.time()
    return {"ok": True}


@router.get("/poll")
async def poll(since: int = 0, authorization: str | None = Header(None)):
    """Long-poll for pending commands.

    `since` is the highest cmd id the agent has already seen. We return
    everything strictly greater. If the queue is empty, we block on the
    waiter Event for up to POLL_SECS — `enqueue()` calls .set() to wake us.
    On timeout we return an empty list; the agent reconnects immediately.
    """
    rig = _auth(authorization)
    s = agent_state.get(rig.id)

    pending = [c for c in s.queue if c["id"] > since]
    if pending:
        return {"commands": pending}

    s.waiter.clear()
    try:
        await asyncio.wait_for(s.waiter.wait(), timeout=POLL_SECS)
    except asyncio.TimeoutError:
        return {"commands": []}

    return {"commands": [c for c in s.queue if c["id"] > since]}


@router.post("/result")
async def post_result(req: Request, authorization: str | None = Header(None)):
    """Receive a command result from the rig agent.

    Removes the command from the queue (so a reconnect doesn't replay it)
    and stashes the result under its id for the dashboard to read.
    Truncation of stdout/stderr is the agent's responsibility — keep this
    endpoint dumb.
    """
    rig = _auth(authorization)
    body = await req.json()
    cmd_id = body.get("id")
    if cmd_id is None:
        raise HTTPException(400, "missing command id")
    s = agent_state.get(rig.id)
    s.results[cmd_id] = {
        "rc": body.get("rc"),
        "stdout": body.get("stdout", ""),
        "stderr": body.get("stderr", ""),
        "received_at": time.time(),
    }
    s.queue = [c for c in s.queue if c["id"] != cmd_id]
    # Wake any wait_for_result() coroutine blocked on this id.
    agent_state.signal_result(rig.id, cmd_id)
    return {"ok": True}
