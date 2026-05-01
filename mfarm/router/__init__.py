"""Router-integration backends.

CatStack uses a pluggable backend to manage port forwarding (and eventually
DHCP reservations) on the operator's router. The default `manual` backend
is a no-op that surfaces instructions for the operator to apply by hand;
real backends like `unifi` use the router's API to create rules
automatically when a rig is added or its Vast port range becomes known.

To add a new backend:
  1. Subclass RouterBackend in mfarm/router/<name>.py
  2. Register the class in BACKENDS below
  3. Add a UI option (later — settings page selector)
"""

from __future__ import annotations

from mfarm.router.base import RouterBackend, ForwardRule, ConfigError, ApplyResult
from mfarm.router.manual import ManualBackend
from mfarm.router.unifi import UnifiBackend

BACKENDS: dict[str, type[RouterBackend]] = {
    "manual": ManualBackend,
    "unifi": UnifiBackend,
}


def get_backend(name: str, config: dict) -> RouterBackend:
    """Construct a backend instance. Raises KeyError on unknown name."""
    cls = BACKENDS[name]
    return cls(config)


__all__ = [
    "RouterBackend", "ForwardRule", "ConfigError", "ApplyResult",
    "ManualBackend", "UnifiBackend", "BACKENDS", "get_backend",
]
