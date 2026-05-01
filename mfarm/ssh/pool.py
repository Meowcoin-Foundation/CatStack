"""SSH connection pool for managing persistent connections to mining rigs."""

from __future__ import annotations

import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import paramiko

from mfarm.config import (
    SSH_CONNECT_TIMEOUT,
    SSH_COMMAND_TIMEOUT,
    SSH_KEEPALIVE_INTERVAL,
    SSH_MAX_WORKERS,
)
from mfarm.db.models import Rig

log = logging.getLogger(__name__)

_pool: SSHConnectionPool | None = None
_pool_lock = threading.Lock()


class SSHConnectionPool:
    def __init__(self):
        self._clients: dict[str, paramiko.SSHClient] = {}
        self._lock = threading.Lock()
        self._keepalive_thread: threading.Thread | None = None
        self._running = False
        self._executor = ThreadPoolExecutor(max_workers=SSH_MAX_WORKERS)
        self._start_keepalive()

    def _start_keepalive(self):
        self._running = True
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, daemon=True, name="ssh-keepalive"
        )
        self._keepalive_thread.start()

    def _keepalive_loop(self):
        while self._running:
            time.sleep(SSH_KEEPALIVE_INTERVAL)
            with self._lock:
                dead = []
                for name, client in self._clients.items():
                    try:
                        transport = client.get_transport()
                        if transport and transport.is_active():
                            transport.send_ignore()
                        else:
                            dead.append(name)
                    except Exception:
                        dead.append(name)
                for name in dead:
                    try:
                        self._clients[name].close()
                    except Exception:
                        pass
                    del self._clients[name]
                    log.debug("Keepalive: dropped stale connection to %s", name)

    def _connect(self, rig: Rig) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": rig.host,
            "port": rig.ssh_port,
            "username": rig.ssh_user,
            "timeout": SSH_CONNECT_TIMEOUT,
            "allow_agent": True,
            "look_for_keys": True,
        }

        if rig.ssh_key_path:
            connect_kwargs["key_filename"] = rig.ssh_key_path

        client.connect(**connect_kwargs)

        # Enable keepalive at the transport level too
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(SSH_KEEPALIVE_INTERVAL)

        return client

    def get(self, rig: Rig) -> paramiko.SSHClient:
        with self._lock:
            client = self._clients.get(rig.name)
            if client is not None:
                transport = client.get_transport()
                if transport and transport.is_active():
                    return client
                # Dead connection, remove it
                try:
                    client.close()
                except Exception:
                    pass
                del self._clients[rig.name]

        # Connect outside the lock to avoid blocking other rigs
        client = self._connect(rig)
        with self._lock:
            self._clients[rig.name] = client
        return client

    def exec(
        self, rig: Rig, command: str, timeout: int = SSH_COMMAND_TIMEOUT
    ) -> tuple[str, str, int]:
        client = self.get(rig)
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            rc = stdout.channel.recv_exit_status()
            return stdout.read().decode("utf-8", errors="replace"), \
                   stderr.read().decode("utf-8", errors="replace"), rc
        except Exception:
            # Connection might be dead, remove from pool and retry once
            with self._lock:
                self._clients.pop(rig.name, None)
            try:
                client.close()
            except Exception:
                pass
            # Retry with fresh connection
            client = self.get(rig)
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            rc = stdout.channel.recv_exit_status()
            return stdout.read().decode("utf-8", errors="replace"), \
                   stderr.read().decode("utf-8", errors="replace"), rc

    def exec_stream(self, rig: Rig, command: str):
        """Execute a command and stream stdout to console in real-time."""
        client = self.get(rig)
        stdin, stdout, stderr = client.exec_command(command)
        try:
            for line in stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
        except KeyboardInterrupt:
            stdout.channel.close()
            raise

    def _sftp_with_retry(self, rig: Rig, op, max_attempts: int = 3):
        """Open SFTP and run `op(sftp)`. Retries up to `max_attempts` times,
        dropping the cached client between attempts. Without this, a cached
        connection going stale (long-running apt ops, mfarm-agent restart,
        network blips) raises an empty paramiko/socket exception that callers
        struggle to debug.

        On final failure raises a wrapped Exception with the type+message of
        the last underlying error, so the API response surfaces something
        actionable instead of an empty string."""
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            client = self.get(rig)
            try:
                sftp = client.open_sftp()
                try:
                    return op(sftp)
                finally:
                    try:
                        sftp.close()
                    except Exception:
                        pass
            except Exception as e:
                last_exc = e
                log.warning(
                    "sftp op on %s failed (attempt %d/%d): %s: %s",
                    rig.name, attempt + 1, max_attempts,
                    type(e).__name__, e or "(no message)",
                )
                with self._lock:
                    self._clients.pop(rig.name, None)
                try:
                    client.close()
                except Exception:
                    pass
                if attempt < max_attempts - 1:
                    time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s backoff
        msg = f"{type(last_exc).__name__}: {last_exc or '(empty)'}"
        raise RuntimeError(f"sftp failed after {max_attempts} attempts on {rig.name}: {msg}")

    def upload(self, rig: Rig, local_path: str, remote_path: str):
        self._sftp_with_retry(rig, lambda s: s.put(local_path, remote_path))

    def upload_string(self, rig: Rig, content: str, remote_path: str):
        """Write a string directly to a remote file."""
        def _write(sftp):
            with sftp.open(remote_path, "w") as f:
                f.write(content)
        self._sftp_with_retry(rig, _write)

    def download(self, rig: Rig, remote_path: str, local_path: str):
        self._sftp_with_retry(rig, lambda s: s.get(remote_path, local_path))

    def exec_parallel(
        self, rigs: list[Rig], command: str, timeout: int = SSH_COMMAND_TIMEOUT
    ) -> dict[str, tuple[str, str, int] | Exception]:
        """Execute a command on multiple rigs in parallel."""
        results: dict[str, tuple[str, str, int] | Exception] = {}
        futures = {
            self._executor.submit(self.exec, rig, command, timeout): rig
            for rig in rigs
        }
        for future in as_completed(futures, timeout=timeout + 10):
            rig = futures[future]
            try:
                results[rig.name] = future.result()
            except Exception as e:
                results[rig.name] = e
        return results

    def poll_stats(
        self, rigs: list[Rig], callback: Callable[[Rig, dict | None, Exception | None], None]
    ):
        """Poll stats.json from all rigs in parallel, calling callback for each."""
        import json

        def _poll_one(rig: Rig):
            try:
                stdout, _, rc = self.exec(rig, "cat /var/run/mfarm/stats.json", timeout=5)
                if rc == 0 and stdout.strip():
                    stats = json.loads(stdout)
                    callback(rig, stats, None)
                else:
                    callback(rig, None, None)
            except Exception as e:
                callback(rig, None, e)

        futures = [self._executor.submit(_poll_one, rig) for rig in rigs]
        for f in as_completed(futures, timeout=15):
            pass  # results delivered via callback

    def close(self, rig_name: str):
        with self._lock:
            client = self._clients.pop(rig_name, None)
        if client:
            try:
                client.close()
            except Exception:
                pass

    def close_all(self):
        self._running = False
        with self._lock:
            for client in self._clients.values():
                try:
                    client.close()
                except Exception:
                    pass
            self._clients.clear()
        self._executor.shutdown(wait=False)


def get_pool() -> SSHConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = SSHConnectionPool()
    return _pool
