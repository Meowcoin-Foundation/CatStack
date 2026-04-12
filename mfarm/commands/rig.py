from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from mfarm.db.connection import get_db
from mfarm.db.models import Rig, Group
from mfarm.targets import resolve_targets

console = Console()


@click.group("rig")
def rig_group():
    """Manage mining rigs."""
    pass


@rig_group.command("add")
@click.argument("name")
@click.argument("host")
@click.option("--port", default=22, help="SSH port")
@click.option("--user", default="root", help="SSH username")
@click.option("--key", default=None, help="Path to SSH private key")
@click.option("--group", "group_name", default=None, help="Assign to group")
@click.option("--notes", default=None, help="Notes about this rig")
def rig_add(name, host, port, user, key, group_name, notes):
    """Add a new rig. NAME is a friendly name, HOST is IP or hostname."""
    db = get_db()

    if Rig.get_by_name(db, name):
        raise click.ClickException(f"Rig '{name}' already exists")

    group_id = None
    if group_name:
        grp = Group.get_by_name(db, group_name)
        if grp is None:
            raise click.ClickException(f"Group '{group_name}' not found. Create it first with: mfarm group create {group_name}")
        group_id = grp.id

    rig = Rig(
        name=name, host=host, ssh_port=port, ssh_user=user,
        ssh_key_path=key, group_id=group_id, notes=notes,
    )
    rig.save(db)
    console.print(f"[green]Added rig '{name}' ({host}:{port})[/green]")


@rig_group.command("remove")
@click.argument("name")
@click.option("--force", is_flag=True, help="Skip confirmation")
def rig_remove(name, force):
    """Remove a rig from management."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if rig is None:
        raise click.ClickException(f"Rig '{name}' not found")

    if not force:
        click.confirm(f"Remove rig '{name}' ({rig.host})?", abort=True)

    rig.delete(db)
    console.print(f"[yellow]Removed rig '{name}'[/yellow]")


@rig_group.command("list")
@click.option("--group", "group_name", default=None, help="Filter by group")
def rig_list(group_name):
    """List all rigs."""
    db = get_db()
    rigs = Rig.get_all(db, group_name=group_name)

    if not rigs:
        console.print("[dim]No rigs configured. Add one with: mfarm rig add <name> <host>[/dim]")
        return

    table = Table(title="Mining Rigs", show_lines=False)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Host", style="white")
    table.add_column("Port", justify="right")
    table.add_column("User", style="dim")
    table.add_column("Group", style="magenta")
    table.add_column("Flight Sheet", style="green")
    table.add_column("OC Profile", style="yellow")
    table.add_column("GPUs")

    for r in rigs:
        gpu_count = str(len(r.gpu_names)) if r.gpu_names else "-"
        table.add_row(
            r.name,
            r.host,
            str(r.ssh_port),
            r.ssh_user,
            r.group_name or "-",
            r.flight_sheet_name or "-",
            r.oc_profile_name or "-",
            gpu_count,
        )

    console.print(table)
    console.print(f"\n[dim]{len(rigs)} rig(s) total[/dim]")


@rig_group.command("info")
@click.argument("name")
def rig_info(name):
    """Show detailed info for a rig."""
    db = get_db()
    rig = Rig.get_by_name(db, name)
    if rig is None:
        raise click.ClickException(f"Rig '{name}' not found")

    console.print(f"\n[bold cyan]{rig.name}[/bold cyan]")
    console.print(f"  Host:          {rig.host}:{rig.ssh_port}")
    console.print(f"  User:          {rig.ssh_user}")
    console.print(f"  SSH Key:       {rig.ssh_key_path or 'default'}")
    console.print(f"  Group:         {rig.group_name or '-'}")
    console.print(f"  Flight Sheet:  {rig.flight_sheet_name or '-'}")
    console.print(f"  OC Profile:    {rig.oc_profile_name or '-'}")
    console.print(f"  Agent:         {rig.agent_version or 'not deployed'}")
    console.print(f"  OS:            {rig.os_info or 'unknown'}")
    console.print(f"  CPU:           {rig.cpu_model or 'unknown'}")

    gpus = rig.gpu_names
    if gpus:
        console.print(f"  GPUs ({len(gpus)}):")
        for i, gpu in enumerate(gpus):
            console.print(f"    [{i}] {gpu}")
    else:
        console.print("  GPUs:          unknown (deploy agent to detect)")

    if rig.notes:
        console.print(f"  Notes:         {rig.notes}")
    console.print()


@rig_group.command("exec")
@click.argument("target")
@click.argument("command")
@click.option("--timeout", default=30, help="Command timeout in seconds")
def rig_exec(target, command, timeout):
    """Execute a command on rig(s). TARGET can be rig name, group:name, or all."""
    from mfarm.ssh.pool import get_pool

    db = get_db()
    rigs = resolve_targets(db, target)
    pool = get_pool()

    for rig in rigs:
        if len(rigs) > 1:
            console.print(f"\n[bold cyan]--- {rig.name} ({rig.host}) ---[/bold cyan]")
        try:
            stdout, stderr, rc = pool.exec(rig, command, timeout=timeout)
            if stdout:
                console.print(stdout.rstrip())
            if stderr:
                console.print(f"[red]{stderr.rstrip()}[/red]")
            if rc != 0:
                console.print(f"[yellow]Exit code: {rc}[/yellow]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")


@rig_group.command("reboot")
@click.argument("target")
@click.option("--force", is_flag=True, help="Skip confirmation")
def rig_reboot(target, force):
    """Reboot rig(s). TARGET can be rig name, group:name, or all."""
    from mfarm.ssh.pool import get_pool

    db = get_db()
    rigs = resolve_targets(db, target)

    if not force:
        names = ", ".join(r.name for r in rigs)
        click.confirm(f"Reboot {len(rigs)} rig(s): {names}?", abort=True)

    pool = get_pool()
    for rig in rigs:
        try:
            pool.exec(rig, "reboot", timeout=5)
            console.print(f"[green]{rig.name}: rebooting[/green]")
        except Exception:
            # Connection drops on reboot, that's expected
            console.print(f"[green]{rig.name}: reboot sent[/green]")


@rig_group.command("shutdown")
@click.argument("target")
@click.option("--force", is_flag=True, help="Skip confirmation")
def rig_shutdown(target, force):
    """Shutdown rig(s). TARGET can be rig name, group:name, or all."""
    from mfarm.ssh.pool import get_pool

    db = get_db()
    rigs = resolve_targets(db, target)

    if not force:
        names = ", ".join(r.name for r in rigs)
        click.confirm(f"Shutdown {len(rigs)} rig(s): {names}?", abort=True)

    pool = get_pool()
    for rig in rigs:
        try:
            pool.exec(rig, "shutdown -h now", timeout=5)
            console.print(f"[yellow]{rig.name}: shutting down[/yellow]")
        except Exception:
            console.print(f"[yellow]{rig.name}: shutdown sent[/yellow]")


@rig_group.command("logs")
@click.argument("name")
@click.option("--miner", "log_type", flag_value="miner", default=True, help="Show miner logs (default)")
@click.option("--agent", "log_type", flag_value="agent", help="Show agent logs")
@click.option("--system", "log_type", flag_value="system", help="Show system logs")
@click.option("--tail", "lines", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def rig_logs(name, log_type, lines, follow):
    """View logs from a rig."""
    from mfarm.ssh.pool import get_pool

    db = get_db()
    rig = Rig.get_by_name(db, name)
    if rig is None:
        raise click.ClickException(f"Rig '{name}' not found")

    pool = get_pool()

    log_paths = {
        "miner": "/var/log/mfarm/miner.log",
        "agent": "/var/log/mfarm/agent.log",
        "system": "",  # use journalctl
    }

    if log_type == "system":
        cmd = f"journalctl -n {lines} --no-pager"
        if follow:
            cmd += " -f"
    else:
        path = log_paths[log_type]
        cmd = f"tail -n {lines} {path}"
        if follow:
            cmd += " -f"

    try:
        if follow:
            console.print(f"[dim]Following {log_type} logs on {name} (Ctrl+C to stop)...[/dim]")
            # For follow mode, use a streaming approach
            pool.exec_stream(rig, cmd)
        else:
            stdout, stderr, rc = pool.exec(rig, cmd, timeout=10)
            if stdout:
                console.print(stdout.rstrip())
            if stderr:
                console.print(f"[red]{stderr.rstrip()}[/red]")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
