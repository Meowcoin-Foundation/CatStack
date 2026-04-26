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

    # Comma-separated list: "rig02,mini01,Octo_Top". Used by the dashboard's
    # pickTarget multi-select for bulk actions. Skip blanks (trailing commas)
    # and report all missing names at once instead of bailing on the first.
    if "," in target:
        names = [n.strip() for n in target.split(",") if n.strip()]
        rigs: list[Rig] = []
        missing: list[str] = []
        for name in names:
            r = Rig.get_by_name(db, name)
            if r is None:
                missing.append(name)
            else:
                rigs.append(r)
        if missing:
            raise click.ClickException(f"Rigs not found: {', '.join(missing)}")
        if not rigs:
            raise click.ClickException("No rigs in target list")
        return rigs

    rig = Rig.get_by_name(db, target)
    if rig is None:
        raise click.ClickException(f"Rig '{target}' not found")
    return [rig]
