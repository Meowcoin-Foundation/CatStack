from __future__ import annotations

import json

import click
from rich.console import Console
from rich.table import Table

from mfarm.db.connection import get_db
from mfarm.db.models import OcProfile
from mfarm.targets import resolve_targets

console = Console()


@click.group("oc")
def oc_group():
    """Manage overclock profiles."""
    pass


@oc_group.command("create")
@click.argument("name")
@click.option("--core-offset", type=int, default=None, help="Core clock offset in MHz (e.g. +150, -100)")
@click.option("--mem-offset", type=int, default=None, help="Memory clock offset in MHz (e.g. +1200)")
@click.option("--power-limit", type=int, default=None, help="Power limit in Watts")
@click.option("--fan-speed", type=int, default=None, help="Fan speed percentage (0-100)")
@click.option("--per-gpu", default=None, help='Per-GPU overrides as JSON: [{"gpu":0,"core_offset":150,...}]')
@click.option("--notes", default=None)
def oc_create(name, core_offset, mem_offset, power_limit, fan_speed, per_gpu, notes):
    """Create an overclock profile."""
    db = get_db()

    if OcProfile.get_by_name(db, name):
        raise click.ClickException(f"OC profile '{name}' already exists")

    if per_gpu:
        try:
            json.loads(per_gpu)
        except json.JSONDecodeError:
            raise click.ClickException("--per-gpu must be valid JSON")

    if fan_speed is not None and not (0 <= fan_speed <= 100):
        raise click.ClickException("Fan speed must be 0-100")

    profile = OcProfile(
        name=name, core_offset=core_offset, mem_offset=mem_offset,
        power_limit=power_limit, fan_speed=fan_speed,
        per_gpu_overrides=per_gpu, notes=notes,
    )
    profile.save(db)

    parts = []
    if core_offset is not None:
        parts.append(f"core {core_offset:+d}")
    if mem_offset is not None:
        parts.append(f"mem {mem_offset:+d}")
    if power_limit is not None:
        parts.append(f"PL {power_limit}W")
    if fan_speed is not None:
        parts.append(f"fan {fan_speed}%")

    console.print(f"[green]Created OC profile '{name}' ({', '.join(parts) or 'empty'})[/green]")


@oc_group.command("edit")
@click.argument("name")
@click.option("--core-offset", type=int, default=None)
@click.option("--mem-offset", type=int, default=None)
@click.option("--power-limit", type=int, default=None)
@click.option("--fan-speed", type=int, default=None)
@click.option("--per-gpu", default=None)
@click.option("--notes", default=None)
def oc_edit(name, **kwargs):
    """Edit an overclock profile."""
    db = get_db()
    profile = OcProfile.get_by_name(db, name)
    if profile is None:
        raise click.ClickException(f"OC profile '{name}' not found")

    changed = []
    for key, val in kwargs.items():
        if val is not None:
            if key == "per_gpu":
                try:
                    json.loads(val)
                except json.JSONDecodeError:
                    raise click.ClickException("--per-gpu must be valid JSON")
                profile.per_gpu_overrides = val
            else:
                setattr(profile, key, val)
            changed.append(key)

    if not changed:
        console.print("[dim]Nothing to update.[/dim]")
        return

    profile.save(db)
    console.print(f"[green]Updated OC profile '{name}': {', '.join(changed)}[/green]")


@oc_group.command("list")
def oc_list():
    """List all overclock profiles."""
    db = get_db()
    profiles = OcProfile.get_all(db)

    if not profiles:
        console.print("[dim]No OC profiles. Create one with: mfarm oc create <name> ...[/dim]")
        return

    table = Table(title="Overclock Profiles")
    table.add_column("Name", style="cyan")
    table.add_column("Core", justify="right")
    table.add_column("Mem", justify="right")
    table.add_column("PL (W)", justify="right")
    table.add_column("Fan (%)", justify="right")
    table.add_column("Per-GPU", justify="center")

    for p in profiles:
        table.add_row(
            p.name,
            f"{p.core_offset:+d}" if p.core_offset is not None else "-",
            f"{p.mem_offset:+d}" if p.mem_offset is not None else "-",
            str(p.power_limit) if p.power_limit is not None else "-",
            str(p.fan_speed) if p.fan_speed is not None else "auto",
            "Y" if p.per_gpu_overrides else "",
        )

    console.print(table)


@oc_group.command("show")
@click.argument("name")
def oc_show(name):
    """Show details of an overclock profile."""
    db = get_db()
    profile = OcProfile.get_by_name(db, name)
    if profile is None:
        raise click.ClickException(f"OC profile '{name}' not found")

    console.print(f"\n[bold cyan]{profile.name}[/bold cyan]")
    console.print(f"  Core Offset:  {profile.core_offset:+d} MHz" if profile.core_offset is not None else "  Core Offset:  -")
    console.print(f"  Mem Offset:   {profile.mem_offset:+d} MHz" if profile.mem_offset is not None else "  Mem Offset:   -")
    console.print(f"  Power Limit:  {profile.power_limit}W" if profile.power_limit is not None else "  Power Limit:  -")
    console.print(f"  Fan Speed:    {profile.fan_speed}%" if profile.fan_speed is not None else "  Fan Speed:    auto")

    if profile.per_gpu:
        console.print("  Per-GPU Overrides:")
        for gpu_oc in profile.per_gpu:
            idx = gpu_oc.get("gpu", "?")
            parts = []
            if "core_offset" in gpu_oc:
                parts.append(f"core {gpu_oc['core_offset']:+d}")
            if "mem_offset" in gpu_oc:
                parts.append(f"mem {gpu_oc['mem_offset']:+d}")
            if "power_limit" in gpu_oc:
                parts.append(f"PL {gpu_oc['power_limit']}W")
            if "fan_speed" in gpu_oc:
                parts.append(f"fan {gpu_oc['fan_speed']}%")
            console.print(f"    GPU {idx}: {', '.join(parts)}")

    if profile.notes:
        console.print(f"  Notes:        {profile.notes}")

    rigs = db.execute("SELECT name FROM rigs WHERE oc_profile_id=?", (profile.id,)).fetchall()
    if rigs:
        names = ", ".join(r["name"] for r in rigs)
        console.print(f"  [dim]Used by: {names}[/dim]")
    console.print()


@oc_group.command("apply")
@click.argument("oc_name")
@click.argument("target")
@click.option("--restart/--no-restart", default=True, help="Restart miner after applying (default: yes)")
def oc_apply(oc_name, target, restart):
    """Apply an OC profile to rig(s). TARGET can be rig name, group:name, or all."""
    from mfarm.ssh.pool import get_pool

    db = get_db()
    profile = OcProfile.get_by_name(db, oc_name)
    if profile is None:
        raise click.ClickException(f"OC profile '{oc_name}' not found")

    rigs = resolve_targets(db, target)
    pool = get_pool()

    console.print(f"[bold]Applying OC profile '{oc_name}' to {len(rigs)} rig(s)...[/bold]\n")

    for rig in rigs:
        console.print(f"[cyan]{rig.name}[/cyan]:")
        try:
            # Update DB
            rig.oc_profile_id = profile.id
            rig.save(db)

            # Read current config, merge OC profile
            stdout, _, rc = pool.exec(rig, "cat /etc/mfarm/config.json", timeout=5)
            if rc == 0 and stdout.strip():
                config = json.loads(stdout)
            else:
                config = {"agent": {"version": "0.1.0"}}

            oc_data = {
                "name": profile.name,
                "core_offset": profile.core_offset,
                "mem_offset": profile.mem_offset,
                "power_limit": profile.power_limit,
                "fan_speed": profile.fan_speed,
            }
            if profile.per_gpu:
                oc_data["per_gpu"] = profile.per_gpu

            config["oc_profile"] = oc_data

            config_json = json.dumps(config, indent=2)
            pool.upload_string(rig, config_json, "/etc/mfarm/config.json")
            console.print(f"  Config updated with OC profile")

            if restart:
                pool.upload_string(rig, "apply_config", "/var/run/mfarm/command")
                console.print(f"  [green]Miner restarting with new OC settings[/green]")

        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")

    console.print()


@oc_group.command("delete")
@click.argument("name")
@click.option("--force", is_flag=True)
def oc_delete(name, force):
    """Delete an overclock profile."""
    db = get_db()
    profile = OcProfile.get_by_name(db, name)
    if profile is None:
        raise click.ClickException(f"OC profile '{name}' not found")

    rigs = db.execute("SELECT name FROM rigs WHERE oc_profile_id=?", (profile.id,)).fetchall()
    if rigs and not force:
        names = ", ".join(r["name"] for r in rigs)
        raise click.ClickException(f"OC profile is used by: {names}. Use --force to delete anyway.")

    if not force:
        click.confirm(f"Delete OC profile '{name}'?", abort=True)

    profile.delete(db)
    console.print(f"[yellow]Deleted OC profile '{name}'[/yellow]")
