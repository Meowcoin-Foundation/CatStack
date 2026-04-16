import click
from rich.console import Console

from mfarm import __version__
from mfarm.commands.rig import rig_group
from mfarm.commands.fleet import flight_group
from mfarm.commands.oc import oc_group
from mfarm.commands.group import group_group
from mfarm.commands.deploy import deploy_group
from mfarm.commands.dashboard_cmd import dashboard_cmd
from mfarm.commands.web_cmd import web_cmd

console = Console()


@click.group()
@click.version_option(__version__, prog_name="mfarm")
def cli():
    """MeowFarm - Mining Farm Management System"""
    pass


# Register all command groups
cli.add_command(rig_group)
cli.add_command(flight_group)
cli.add_command(oc_group)
cli.add_command(group_group)
cli.add_command(deploy_group)
cli.add_command(dashboard_cmd)
cli.add_command(web_cmd)


@cli.command("status")
def status():
    """Quick status of all rigs."""
    from mfarm.db.connection import get_db
    from mfarm.db.models import Rig
    from mfarm.ssh.pool import get_pool

    db = get_db()
    rigs = Rig.get_all(db)

    if not rigs:
        console.print("[dim]No rigs configured.[/dim]")
        return

    pool = get_pool()
    console.print(f"[bold]Checking {len(rigs)} rig(s)...[/bold]\n")

    for rig in rigs:
        try:
            stdout, _, rc = pool.exec(rig, "cat /var/run/mfarm/stats.json", timeout=5)
            if rc == 0 and stdout.strip():
                import json
                stats = json.loads(stdout)
                miner = stats.get("miner", {})
                hr = miner.get("hashrate", 0)
                algo = miner.get("algo", "?")
                running = miner.get("running", False)
                status_str = "[green]MINING[/green]" if running else "[yellow]IDLE[/yellow]"
                console.print(f"  {rig.name:20s} {status_str}  {_format_hashrate(hr):>12s}  {algo}")
            else:
                console.print(f"  {rig.name:20s} [blue]ONLINE[/blue]  (no agent stats)")
        except Exception:
            console.print(f"  {rig.name:20s} [red]OFFLINE[/red]")

    console.print()


def _format_hashrate(h: float) -> str:
    if h >= 1e12:
        return f"{h/1e12:.2f} TH/s"
    if h >= 1e9:
        return f"{h/1e9:.2f} GH/s"
    if h >= 1e6:
        return f"{h/1e6:.2f} MH/s"
    if h >= 1e3:
        return f"{h/1e3:.2f} KH/s"
    return f"{h:.2f} H/s"
