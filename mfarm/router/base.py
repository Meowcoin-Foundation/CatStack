"""RouterBackend abstract base + shared types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when a backend's config dict is missing/invalid fields."""


@dataclass
class ForwardRule:
    """A port-forward rule keyed by rig name.

    The rig name is the stable identifier; `internal_ip` may shift (auto-heal
    handles drift, then the backend reapplies on update). Port range is
    `port_lo`..`port_hi` inclusive."""
    rig_name: str
    internal_ip: str
    port_lo: int
    port_hi: int
    protocol: str = "tcp_udp"  # tcp / udp / tcp_udp


@dataclass
class ApplyResult:
    """Outcome of an apply / sync operation. `messages` are operator-facing
    strings the dashboard can render; `ok` is the overall status."""
    ok: bool
    messages: list[str]


class RouterBackend(ABC):
    """One implementation per router vendor. All methods must be idempotent —
    callers may invoke `apply_rule` repeatedly with the same input. Backends
    that can't do something (e.g. manual mode) should still return ApplyResult
    with informative messages instead of raising."""

    name: str  # short identifier matching BACKENDS key
    requires_credentials: bool = False  # True if config needs user/pass/token

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def validate_config(self) -> None:
        """Raise ConfigError if self.config is missing required keys.
        Backends with no required fields should still implement (no-op)."""

    @abstractmethod
    def test_connection(self) -> ApplyResult:
        """Probe the router. Returns ok=True if backend can reach + auth.
        Manual backend returns ok=True trivially."""

    @abstractmethod
    def apply_rule(self, rule: ForwardRule) -> ApplyResult:
        """Create or update a port-forward rule. Idempotent."""

    @abstractmethod
    def remove_rule(self, rig_name: str) -> ApplyResult:
        """Remove the port-forward rule keyed by rig_name. Idempotent —
        no-op if no such rule."""

    def list_rules(self) -> list[ForwardRule]:
        """Return rules currently in the router that this backend manages.
        Default: empty (manual backends can't introspect)."""
        return []
