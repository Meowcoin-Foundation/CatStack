"""In-memory state for the rig agent push/poll transport.

This module owns the per-rig "latest stats" cache and the pending command
queue. It exists so dashboard reads can be a dict lookup instead of an SSH
exec — the SSH-pull path through `mfarm/ssh/pool.py` is the failure mode
that wedged the dashboard on 2026-05-03 (see pool.py:170).

Authority model:
  - The rig's agent is the source of truth for `last_stats` (it pushes).
  - The dashboard is the source of truth for `queue` (it enqueues).
  - Both sides can write to `results` (agent posts results, dashboard reads).

Lifetime: process-local. State resets on server restart, which is fine —
agents will re-push within their stats interval (~2s by default).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class _RigState:
    last_stats: dict | None = None
    last_seen: float = 0.0
    queue: list[dict] = field(default_factory=list)        # [{id, cmd, args}, ...]
    next_id: int = 1
    waiter: asyncio.Event = field(default_factory=asyncio.Event)
    results: dict[int, dict] = field(default_factory=dict)  # cmd_id -> {rc, stdout, stderr}
    # Per-cmd-id Events that wait_for_result waiters block on; post_result
    # endpoint sets the matching Event after writing to `results`. Created
    # lazily by wait_for_result and removed in its finally clause so a
    # never-collected result doesn't leak Event objects.
    result_waiters: dict[int, asyncio.Event] = field(default_factory=dict)


_state: dict[int, _RigState] = {}   # rig_id -> _RigState


def get(rig_id: int) -> _RigState:
    """Get or lazily create the state slot for a rig.

    Race-safe under CPython's GIL: concurrent first-time access to the same
    rig may construct a transient _RigState that gets discarded by
    setdefault, but every caller still receives the SAME slot once the dict
    settles."""
    s = _state.get(rig_id)
    if s is None:
        s = _state.setdefault(rig_id, _RigState())
    return s


def stats_age(rig_id: int) -> float:
    """Seconds since this rig last pushed stats. `inf` if never seen."""
    s = _state.get(rig_id)
    return time.time() - s.last_seen if s and s.last_seen else float("inf")


def enqueue(rig_id: int, cmd: str, args: dict | None = None) -> int:
    """Append a command to a rig's queue and wake any waiting long-poll.

    Returns the assigned command id; the caller can read `state.results[id]`
    later to fetch the agent's response, or use `wait_for_result()` to block
    until it arrives. If no agent is currently polling, the command sits in
    the queue until the next poll comes in (commands are durable across
    reconnects within the process lifetime, but lost on server restart —
    okay for restart/reboot/OC commands which the operator can re-issue)."""
    s = get(rig_id)
    cid = s.next_id
    s.next_id += 1
    s.queue.append({"id": cid, "cmd": cmd, "args": args or {}})
    s.waiter.set()
    return cid


async def wait_for_result(rig_id: int, cmd_id: int, timeout: float = 30) -> dict | None:
    """Block until the agent posts a result for `cmd_id`, or `timeout` seconds.

    Returns the result dict (`{rc, stdout, stderr, received_at}`) on success,
    or None on timeout. The result remains in `state.results[cmd_id]` for any
    later inspection; cleanup is the caller's choice (it's small).

    If the result has already arrived by the time we're called (fast agent),
    we short-circuit and return immediately."""
    s = get(rig_id)
    if cmd_id in s.results:
        return s.results[cmd_id]
    ev = s.result_waiters.setdefault(cmd_id, asyncio.Event())
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        # Remove the waiter regardless of outcome — a stale Event is harmless
        # but a leaked dict entry per timed-out command isn't.
        s.result_waiters.pop(cmd_id, None)
    return s.results.get(cmd_id)


async def enqueue_and_wait(rig_id: int, cmd: str, args: dict | None = None,
                           timeout: float = 30) -> dict | None:
    """Convenience: enqueue a command and wait synchronously for its result.

    Returns the same shape as `wait_for_result`. Use this for endpoints that
    must return the agent's stdout/rc to the caller (exec, log fetch, …).
    For fire-and-forget commands (reboot, restart_miner) just call enqueue."""
    cid = enqueue(rig_id, cmd, args)
    return await wait_for_result(rig_id, cid, timeout)


def signal_result(rig_id: int, cmd_id: int) -> None:
    """Wake any task awaiting `cmd_id`. Called by the post_result endpoint
    after the result has been written to `state.results`. No-op if no
    waiter exists (the agent posted before anyone called wait_for_result)."""
    s = _state.get(rig_id)
    if s is None:
        return
    ev = s.result_waiters.get(cmd_id)
    if ev is not None:
        ev.set()
