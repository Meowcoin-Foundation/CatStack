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


@rig_group.command("pilot")
@click.argument("name")
@click.option("--rotate-existing", is_flag=True,
              help="Force re-issue even if a token is already deployed.")
def rig_pilot(name, rotate_existing):
    """Enroll a rig in the agent push transport.

    Rotates a fresh agent token, writes it into /etc/mfarm/config.json on
    the rig, restarts mfarm-agent, and verifies the agent's journal shows
    "Push transport enabled". Idempotent — safe to re-run; each run issues
    a new token (the previous one is invalidated server-side immediately).

    Pre-requisite: the rig must already be running mfarm-agent v0.2.0+.
    The fleet auto-updates from /api/agent/bundle every 5 min, so you
    typically just bump the project VERSION, wait, then run this.
    """
    import json as _json
    import secrets as _secrets
    import time as _time

    from mfarm.ssh.pool import get_pool

    db = get_db()
    rig = Rig.get_by_name(db, name)
    if rig is None:
        raise click.ClickException(f"Rig '{name}' not found")

    if rig.agent_token and not rotate_existing:
        # Don't silently rotate — the previous token may be in someone's
        # config and we'd lock them out without warning. The flag opt-in
        # makes the rotation explicit.
        if not click.confirm(
            f"Rig '{name}' already has a token deployed. Rotate it?",
            default=False,
        ):
            raise click.Abort()

    pool = get_pool()
    console.print(f"[bold]Piloting {rig.name} ({rig.host})...[/bold]")

    # 1. Pre-flight: confirm the rig is running an agent that knows about
    # agent_token. Greps the live agent file for the VERSION line; the
    # auto-updater swaps that file in place when the bundle pulls a newer
    # release. If it's still 0.1.x, refuse — deploying a token to an old
    # agent would have no effect and the operator would see stats fall
    # through to SSH with no signal that anything was wrong.
    try:
        out, _, rc = pool.exec(
            rig,
            "grep -m1 '^VERSION = ' /opt/mfarm/mfarm-agent.py",
            timeout=10,
        )
    except Exception as e:
        raise click.ClickException(f"SSH probe failed: {e}")
    if rc != 0 or "VERSION = " not in out:
        raise click.ClickException(
            "Could not read agent version from rig — is /opt/mfarm/mfarm-agent.py present?"
        )
    version = out.split('"')[1] if '"' in out else "?"
    console.print(f"  rig agent version: [cyan]{version}[/cyan]")
    if not version.startswith(("0.2.", "0.3.", "0.4.", "0.5.")):
        # Soft floor at 0.2.x; bump this list when major-versioning the
        # transport. Anything lower lacks the push_client wiring.
        raise click.ClickException(
            f"Rig is running agent {version}; need 0.2.0+ for push transport. "
            "Bump the project VERSION and wait ~5 min for the auto-updater."
        )

    # 2. Generate token, persist server-side first. If the rig restart
    # later fails, the DB still has the live token — operator can retry.
    token = _secrets.token_urlsafe(32)
    Rig.set_agent_token(db, rig.id, token)
    console.print(f"  issued token: [dim]{token[:8]}…[/dim] (32 bytes)")

    # 3. Read the rig's current config, inject agent_token, write back.
    # Atomic via /etc/mfarm/config.json.tmp + mv, so a partial write
    # can't leave the agent reading invalid JSON on its next reload.
    try:
        out, _, rc = pool.exec(rig, "cat /etc/mfarm/config.json", timeout=10)
    except Exception as e:
        raise click.ClickException(f"Failed to read rig config: {e}")
    if rc != 0:
        # File missing is non-fatal — start from empty.
        config = {}
    else:
        try:
            config = _json.loads(out) if out.strip() else {}
        except Exception as e:
            raise click.ClickException(f"Rig config is not valid JSON: {e}")
    config["agent_token"] = token
    new_content = _json.dumps(config, indent=2)
    try:
        pool.upload_string(rig, new_content, "/etc/mfarm/config.json.tmp")
        pool.exec(
            rig,
            "mv /etc/mfarm/config.json.tmp /etc/mfarm/config.json",
            timeout=5,
        )
    except Exception as e:
        raise click.ClickException(f"Failed to deploy config: {e}")
    console.print("  config deployed")

    # 4. Restart the agent so it picks up the new token. Don't use SIGHUP
    # — the agent reloads config only on `apply_config` command, and that
    # path doesn't re-init push_client. Service restart is the simplest
    # reliable trigger.
    try:
        pool.exec(rig, "systemctl restart mfarm-agent", timeout=15)
    except Exception as e:
        raise click.ClickException(f"Failed to restart mfarm-agent: {e}")
    console.print("  mfarm-agent restarted")

    # 5. Verify by tailing the agent's journal. The agent logs "Push
    # transport enabled" exactly once on startup when the token loaded
    # successfully. Wait a few seconds for systemd to bring it up.
    _time.sleep(4)
    try:
        out, _, _ = pool.exec(
            rig,
            "journalctl -u mfarm-agent -n 50 --no-pager",
            timeout=10,
        )
    except Exception as e:
        console.print(f"  [yellow]could not read journal: {e}[/yellow]")
        return
    if "Push transport enabled" in out:
        console.print(f"[green]  pilot OK — {rig.name} is now on the push transport[/green]")
    else:
        console.print(
            "[yellow]  agent did not log 'Push transport enabled' — "
            "check `journalctl -u mfarm-agent` on the rig manually[/yellow]"
        )


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
