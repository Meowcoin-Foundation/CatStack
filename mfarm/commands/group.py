from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from mfarm.db.connection import get_db
from mfarm.db.models import Group, Rig

console = Console()


@click.group("group")
def group_group():
    """Manage rig groups."""
    pass


@group_group.command("create")
@click.argument("name")
@click.option("--notes", default=None)
def group_create(name, notes):
    """Create a new rig group."""
    db = get_db()
    if Group.get_by_name(db, name):
        raise click.ClickException(f"Group '{name}' already exists")

    grp = Group(name=name, notes=notes)
    grp.save(db)
    console.print(f"[green]Created group '{name}'[/green]")


@group_group.command("delete")
@click.argument("name")
@click.option("--force", is_flag=True)
def group_delete(name, force):
    """Delete a rig group. Rigs in the group become ungrouped."""
    db = get_db()
    grp = Group.get_by_name(db, name)
    if grp is None:
        raise click.ClickException(f"Group '{name}' not found")

    rigs = Rig.get_all(db, group_name=name)
    if rigs and not force:
        names = ", ".join(r.name for r in rigs)
        click.confirm(f"Group has {len(rigs)} rig(s): {names}. They will become ungrouped. Continue?", abort=True)

    grp.delete(db)
    console.print(f"[yellow]Deleted group '{name}'[/yellow]")


@group_group.command("add-rig")
@click.argument("group_name")
@click.argument("rig_name")
def group_add_rig(group_name, rig_name):
    """Add a rig to a group."""
    db = get_db()
    grp = Group.get_by_name(db, group_name)
    if grp is None:
        raise click.ClickException(f"Group '{group_name}' not found")

    rig = Rig.get_by_name(db, rig_name)
    if rig is None:
        raise click.ClickException(f"Rig '{rig_name}' not found")

    if rig.group_id == grp.id:
        console.print(f"[dim]Rig '{rig_name}' is already in group '{group_name}'[/dim]")
        return

    old_group = rig.group_name
    rig.group_id = grp.id
    rig.save(db)

    msg = f"[green]Added '{rig_name}' to group '{group_name}'[/green]"
    if old_group:
        msg += f" [dim](was in '{old_group}')[/dim]"
    console.print(msg)


@group_group.command("remove-rig")
@click.argument("group_name")
@click.argument("rig_name")
def group_remove_rig(group_name, rig_name):
    """Remove a rig from a group."""
    db = get_db()
    grp = Group.get_by_name(db, group_name)
    if grp is None:
        raise click.ClickException(f"Group '{group_name}' not found")

    rig = Rig.get_by_name(db, rig_name)
    if rig is None:
        raise click.ClickException(f"Rig '{rig_name}' not found")

    if rig.group_id != grp.id:
        raise click.ClickException(f"Rig '{rig_name}' is not in group '{group_name}'")

    rig.group_id = None
    rig.save(db)
    console.print(f"[yellow]Removed '{rig_name}' from group '{group_name}'[/yellow]")


@group_group.command("list")
def group_list():
    """List all groups and their rigs."""
    db = get_db()
    groups = Group.get_all(db)

    if not groups:
        console.print("[dim]No groups. Create one with: mfarm group create <name>[/dim]")
        return

    table = Table(title="Rig Groups")
    table.add_column("Group", style="magenta")
    table.add_column("Rigs", justify="right")
    table.add_column("Members")
    table.add_column("Notes", style="dim")

    for grp in groups:
        rigs = Rig.get_all(db, group_name=grp.name)
        members = ", ".join(r.name for r in rigs) if rigs else "-"
        table.add_row(grp.name, str(len(rigs)), members, grp.notes or "")

    console.print(table)

    # Also show ungrouped rigs
    all_rigs = Rig.get_all(db)
    ungrouped = [r for r in all_rigs if r.group_id is None]
    if ungrouped:
        names = ", ".join(r.name for r in ungrouped)
        console.print(f"\n[dim]Ungrouped rigs: {names}[/dim]")
