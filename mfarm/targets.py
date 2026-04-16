"""Resolve target specifiers like 'rig-name', 'group:gpu-rigs', or 'all' to lists of Rigs."""

from __future__ import annotations

import sqlite3

import click

from mfarm.db.models import Rig


def resolve_targets(db: sqlite3.Connection, target: str) -> list[Rig]:
    if target == "all":
        rigs = Rig.get_all(db)
        if not rigs:
            raise click.ClickException("No rigs configured")
        return rigs

    if target.startswith("group:"):
        group_name = target[6:]
        rigs = Rig.get_all(db, group_name=group_name)
        if not rigs:
            raise click.ClickException(f"No rigs in group '{group_name}' (or group doesn't exist)")
        return rigs

    rig = Rig.get_by_name(db, target)
    if rig is None:
        raise click.ClickException(f"Rig '{target}' not found")
    return [rig]
