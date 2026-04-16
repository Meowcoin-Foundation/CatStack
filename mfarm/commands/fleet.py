from __future__ import annotations

import json

import click
from rich.console import Console
from rich.table import Table

from mfarm.db.connection import get_db
from mfarm.db.models import FlightSheet, Rig
from mfarm.miners.registry import get_miner, list_miners
from mfarm.targets import resolve_targets

console = Console()


@click.group("flight")
def flight_group():
    """Manage flight sheets (mining configurations)."""
    pass


@flight_group.command("create")
@click.argument("name")
@click.option("--coin", required=True, help="Coin ticker (e.g. MEWC, LKPEPE)")
@click.option("--algo", required=True, help="Algorithm (e.g. scrypt, yescryptR32)")
@click.option("--miner", required=True, help="Miner software (ccminer, trex, lolminer, cpuminer-opt, xmrig)")
@click.option("--pool", "pool_url", required=True, help="Pool URL (stratum+tcp://...)")
@click.option("--wallet", required=True, help="Payout wallet address")
@click.option("--worker", "worker_template", default="%HOSTNAME%", help="Worker name template (default: %%HOSTNAME%%)")
@click.option("--password", default="x", help="Pool password (default: x)")
@click.option("--pool2", "pool_url2", default=None, help="Backup pool URL")
@click.option("--extra-args", default="", help="Extra arguments passed to miner")
@click.option("--solo", is_flag=True, help="Solo mining mode (GBT)")
@click.option("--rpc-user", "solo_rpc_user", default=None, help="Solo: RPC username")
@click.option("--rpc-pass", "solo_rpc_pass", default=None, help="Solo: RPC password")
@click.option("--coinbase-addr", default=None, help="Solo: coinbase address")
@click.option("--notes", default=None)
def flight_create(name, coin, algo, miner, pool_url, wallet, worker_template,
                  password, pool_url2, extra_args, solo, solo_rpc_user,
                  solo_rpc_pass, coinbase_addr, notes):
    """Create a new flight sheet."""
    db = get_db()

    if FlightSheet.get_by_name(db, name):
        raise click.ClickException(f"Flight sheet '{name}' already exists")

    miner_def = get_miner(miner)
    if not miner_def:
        available = ", ".join(m.name for m in list_miners())
        raise click.ClickException(f"Unknown miner '{miner}'. Available: {available}")

    if algo not in miner_def.supported_algos:
        console.print(f"[yellow]Warning: '{algo}' not in known algos for {miner}. Proceeding anyway.[/yellow]")

    if solo and not miner_def.supports_solo:
        raise click.ClickException(f"Miner '{miner}' does not support solo mining (GBT)")

    fs = FlightSheet(
        name=name, coin=coin.upper(), algo=algo, miner=miner,
        pool_url=pool_url, pool_url2=pool_url2, wallet=wallet,
        worker_template=worker_template, password=password,
        extra_args=extra_args, is_solo=1 if solo else 0,
        solo_rpc_user=solo_rpc_user, solo_rpc_pass=solo_rpc_pass,
        coinbase_addr=coinbase_addr, notes=notes,
    )
    fs.save(db)
    console.print(f"[green]Created flight sheet '{name}' ({coin} / {algo} / {miner})[/green]")


@flight_group.command("edit")
@click.argument("name")
@click.option("--coin", default=None)
@click.option("--algo", default=None)
@click.option("--miner", default=None)
@click.option("--pool", "pool_url", default=None)
@click.option("--wallet", default=None)
@click.option("--worker", "worker_template", default=None)
@click.option("--password", default=None)
@click.option("--pool2", "pool_url2", default=None)
@click.option("--extra-args", default=None)
@click.option("--solo/--no-solo", default=None)
@click.option("--rpc-user", "solo_rpc_user", default=None)
@click.option("--rpc-pass", "solo_rpc_pass", default=None)
@click.option("--coinbase-addr", default=None)
@click.option("--notes", default=None)
def flight_edit(name, **kwargs):
    """Edit an existing flight sheet."""
    db = get_db()
    fs = FlightSheet.get_by_name(db, name)
    if fs is None:
        raise click.ClickException(f"Flight sheet '{name}' not found")

    changed = []
    for key, val in kwargs.items():
        if val is not None:
            if key == "solo":
                setattr(fs, "is_solo", 1 if val else 0)
            elif key == "pool_url":
                fs.pool_url = val
            elif key == "pool_url2":
                fs.pool_url2 = val
            elif key == "worker_template":
                fs.worker_template = val
            elif key == "solo_rpc_user":
                fs.solo_rpc_user = val
            elif key == "solo_rpc_pass":
                fs.solo_rpc_pass = val
            elif key == "coinbase_addr":
                fs.coinbase_addr = val
            else:
                setattr(fs, key, val)
            changed.append(key)

    if not changed:
        console.print("[dim]Nothing to update.[/dim]")
        return

    fs.save(db)
    console.print(f"[green]Updated flight sheet '{name}': {', '.join(changed)}[/green]")


@flight_group.command("list")
def flight_list():
    """List all flight sheets."""
    db = get_db()
    sheets = FlightSheet.get_all(db)

    if not sheets:
        console.print("[dim]No flight sheets. Create one with: mfarm flight create <name> ...[/dim]")
        return

    table = Table(title="Flight Sheets")
    table.add_column("Name", style="cyan")
    table.add_column("Coin", style="yellow")
    table.add_column("Algo")
    table.add_column("Miner", style="green")
    table.add_column("Pool")
    table.add_column("Solo", justify="center")

    for fs in sheets:
        pool_short = fs.pool_url[:40] + "..." if len(fs.pool_url) > 40 else fs.pool_url
        table.add_row(
            fs.name, fs.coin, fs.algo, fs.miner, pool_short,
            "Y" if fs.is_solo else "",
        )

    console.print(table)


@flight_group.command("show")
@click.argument("name")
def flight_show(name):
    """Show details of a flight sheet."""
    db = get_db()
    fs = FlightSheet.get_by_name(db, name)
    if fs is None:
        raise click.ClickException(f"Flight sheet '{name}' not found")

    console.print(f"\n[bold cyan]{fs.name}[/bold cyan]")
    console.print(f"  Coin:       {fs.coin}")
    console.print(f"  Algorithm:  {fs.algo}")
    console.print(f"  Miner:      {fs.miner}" + (f" v{fs.miner_version}" if fs.miner_version else ""))
    console.print(f"  Pool:       {fs.pool_url}")
    if fs.pool_url2:
        console.print(f"  Pool 2:     {fs.pool_url2}")
    console.print(f"  Wallet:     {fs.wallet}")
    console.print(f"  Worker:     {fs.worker_template}")
    console.print(f"  Password:   {fs.password}")
    if fs.extra_args:
        console.print(f"  Extra Args: {fs.extra_args}")
    if fs.is_solo:
        console.print(f"  [bold]Solo Mining (GBT)[/bold]")
        console.print(f"    RPC User:     {fs.solo_rpc_user}")
        console.print(f"    RPC Pass:     {'*' * len(fs.solo_rpc_pass) if fs.solo_rpc_pass else '-'}")
        console.print(f"    Coinbase:     {fs.coinbase_addr}")
    if fs.notes:
        console.print(f"  Notes:      {fs.notes}")

    # Show which rigs use this flight sheet
    rigs = db.execute(
        "SELECT name FROM rigs WHERE flight_sheet_id=?", (fs.id,)
    ).fetchall()
    if rigs:
        names = ", ".join(r["name"] for r in rigs)
        console.print(f"  [dim]Used by: {names}[/dim]")
    console.print()


@flight_group.command("apply")
@click.argument("fs_name")
@click.argument("target")
@click.option("--restart/--no-restart", default=True, help="Restart miner after applying (default: yes)")
def flight_apply(fs_name, target, restart):
    """Apply a flight sheet to rig(s). TARGET can be rig name, group:name, or all."""
    from mfarm.ssh.pool import get_pool

    db = get_db()
    fs = FlightSheet.get_by_name(db, fs_name)
    if fs is None:
        raise click.ClickException(f"Flight sheet '{fs_name}' not found")

    rigs = resolve_targets(db, target)
    pool = get_pool()

    console.print(f"[bold]Applying flight sheet '{fs_name}' to {len(rigs)} rig(s)...[/bold]\n")

    for rig in rigs:
        console.print(f"[cyan]{rig.name}[/cyan]:")
        try:
            # Update DB
            rig.flight_sheet_id = fs.id
            rig.save(db)

            # Build config for this rig
            hostname = rig.name
            worker = fs.worker_template.replace("%HOSTNAME%", hostname).replace("%RIGNAME%", rig.name)

            miner_def = get_miner(fs.miner)
            api_port = miner_def.default_api_port if miner_def else 4068

            # Read current config from rig, merge flight sheet
            stdout, _, rc = pool.exec(rig, "cat /etc/mfarm/config.json", timeout=5)
            if rc == 0 and stdout.strip():
                config = json.loads(stdout)
            else:
                config = {
                    "agent": {"version": "0.1.0", "stats_interval": 5, "watchdog_interval": 30,
                              "max_gpu_temp": 90, "critical_gpu_temp": 95},
                    "miner_paths": {},
                    "api_ports": {"ccminer": 4068, "trex": 4067, "lolminer": 44444,
                                  "cpuminer-opt": 4048, "xmrig": 44445},
                }

            config["flight_sheet"] = {
                "name": fs.name,
                "coin": fs.coin,
                "algo": fs.algo,
                "miner": fs.miner,
                "miner_version": fs.miner_version,
                "pool_url": fs.pool_url,
                "pool_url2": fs.pool_url2,
                "wallet": fs.wallet,
                "worker": worker,
                "password": fs.password,
                "extra_args": fs.extra_args,
                "is_solo": bool(fs.is_solo),
                "solo_rpc_user": fs.solo_rpc_user,
                "solo_rpc_pass": fs.solo_rpc_pass,
                "coinbase_addr": fs.coinbase_addr,
            }

            # Upload new config
            config_json = json.dumps(config, indent=2)
            pool.upload_string(rig, config_json, "/etc/mfarm/config.json")
            console.print(f"  Config updated ({fs.coin} / {fs.algo} / {fs.miner})")

            # Signal agent to reload
            if restart:
                pool.upload_string(rig, "apply_config", "/var/run/mfarm/command")
                console.print(f"  [green]Miner restarting with new config[/green]")
            else:
                console.print(f"  [yellow]Config saved (miner not restarted)[/yellow]")

        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")

    console.print()


@flight_group.command("delete")
@click.argument("name")
@click.option("--force", is_flag=True)
def flight_delete(name, force):
    """Delete a flight sheet."""
    db = get_db()
    fs = FlightSheet.get_by_name(db, name)
    if fs is None:
        raise click.ClickException(f"Flight sheet '{name}' not found")

    rigs = db.execute("SELECT name FROM rigs WHERE flight_sheet_id=?", (fs.id,)).fetchall()
    if rigs and not force:
        names = ", ".join(r["name"] for r in rigs)
        raise click.ClickException(f"Flight sheet is used by: {names}. Use --force to delete anyway.")

    if not force:
        click.confirm(f"Delete flight sheet '{name}'?", abort=True)

    fs.delete(db)
    console.print(f"[yellow]Deleted flight sheet '{name}'[/yellow]")
