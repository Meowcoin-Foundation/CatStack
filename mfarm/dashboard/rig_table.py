"""All-rigs summary table for the dashboard."""

from __future__ import annotations

from rich.table import Table

from mfarm.dashboard.formatters import (
    format_hashrate, format_temps_colored, format_fans,
    format_power, format_uptime, status_icon, total_power,
)


def build_rig_table(
    rig_stats: dict[str, tuple[dict | None, Exception | None]],
    title: str = "MeowFarm Dashboard",
) -> Table:
    """Build the main dashboard table from collected rig stats.

    rig_stats: dict mapping rig_name -> (stats_dict_or_None, error_or_None)
    """
    # Compute totals
    total_hr = 0.0
    online = 0
    total_rigs = len(rig_stats)
    total_pwr = 0.0

    for stats, err in rig_stats.values():
        if stats and not err:
            online += 1
            miner = stats.get("miner", {})
            total_hr += miner.get("hashrate", 0) or 0
            total_pwr += total_power(stats.get("gpus", []))

    table = Table(
        title=f"{title}  |  {online}/{total_rigs} online  |  {format_hashrate(total_hr)}  |  {format_power(total_pwr)}",
        show_lines=False,
        expand=True,
        padding=(0, 1),
    )

    table.add_column("Rig", style="cyan", no_wrap=True, min_width=12)
    table.add_column("Status", justify="center", min_width=6)
    table.add_column("Hashrate", justify="right", min_width=12)
    table.add_column("Algo", min_width=8)
    table.add_column("Temps", min_width=10)
    table.add_column("Fans", min_width=8)
    table.add_column("Power", justify="right", min_width=6)
    table.add_column("Uptime", justify="right", min_width=7)
    table.add_column("Shares", min_width=14)

    for rig_name in sorted(rig_stats.keys()):
        stats, err = rig_stats[rig_name]
        icon = status_icon(stats, err)

        if err or stats is None:
            table.add_row(rig_name, icon, "-", "-", "-", "-", "-", "-", "-")
            continue

        miner = stats.get("miner", {})
        gpus = stats.get("gpus", [])

        hr = format_hashrate(miner.get("hashrate"))
        algo = miner.get("algo", "-")
        temps = format_temps_colored(gpus)
        fans = format_fans(gpus)
        pwr = format_power(total_power(gpus))
        up = format_uptime(stats.get("uptime_secs"))

        # Shares
        acc = miner.get("accepted", 0)
        rej = miner.get("rejected", 0)
        shares = f"{acc}/{rej}" if acc or rej else "-"

        table.add_row(rig_name, icon, hr, algo, temps, fans, pwr, up, shares)

    return table
