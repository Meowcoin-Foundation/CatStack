"""Single-rig detail panel for the dashboard."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mfarm.dashboard.formatters import (
    format_hashrate, format_power, format_uptime,
    temp_color, share_ratio,
)


def build_rig_detail(rig_name: str, stats: dict | None, rig_info: dict | None = None) -> Panel:
    """Build a detailed panel for a single rig.

    rig_info: optional dict with DB fields (flight_sheet_name, oc_profile_name, host, etc.)
    """
    if stats is None:
        return Panel("[red]No stats available[/red]", title=rig_name, border_style="red")

    miner = stats.get("miner", {})
    gpus = stats.get("gpus", [])
    cpu = stats.get("cpu", {})
    system = stats.get("system", {})

    # Header info
    host = rig_info.get("host", "?") if rig_info else "?"
    uptime = format_uptime(stats.get("uptime_secs"))
    agent_ver = stats.get("agent_version", "?")

    header = f"{rig_name} ({host})  |  Uptime: {uptime}  |  Agent: v{agent_ver}"

    # Flight sheet / OC info
    fs_name = rig_info.get("flight_sheet_name", "-") if rig_info else "-"
    oc_name = rig_info.get("oc_profile_name", "-") if rig_info else "-"
    subheader = f"Flight Sheet: {fs_name}  |  OC Profile: {oc_name}"

    # GPU table
    gpu_table = Table(show_lines=False, expand=True, padding=(0, 1))
    gpu_table.add_column("GPU", justify="right", style="dim", width=4)
    gpu_table.add_column("Name", min_width=16)
    gpu_table.add_column("Temp", justify="right", width=5)
    gpu_table.add_column("Fan", justify="right", width=5)
    gpu_table.add_column("Core", justify="right", width=6)
    gpu_table.add_column("Mem", justify="right", width=6)
    gpu_table.add_column("Power", justify="right", width=6)
    gpu_table.add_column("Hash", justify="right", width=10)

    for g in gpus:
        t = g.get("temp")
        tc = temp_color(t)
        gpu_table.add_row(
            str(g.get("index", "?")),
            g.get("name", "Unknown"),
            Text(f"{t}C" if t else "-", style=tc),
            f"{g.get('fan', '-')}%" if g.get("fan") is not None else "-",
            f"{g.get('core_clock', '-')}",
            f"{g.get('mem_clock', '-')}",
            format_power(g.get("power_draw")),
            format_hashrate(g.get("hashrate")),
        )

    # Miner info line
    miner_name = miner.get("name", "?")
    miner_ver = miner.get("version", "")
    pool = miner.get("pool", "?")
    shares = share_ratio(stats)

    miner_line = f"Miner: {miner_name}"
    if miner_ver:
        miner_line += f" v{miner_ver}"
    miner_line += f"  |  Pool: {pool}"
    shares_line = f"Shares: {shares}  |  Hashrate: {format_hashrate(miner.get('hashrate'))}"

    # CPU info
    cpu_line = ""
    if cpu:
        cpu_line = f"CPU: {cpu.get('model', '?')}"
        if cpu.get("temp"):
            cpu_line += f"  |  Temp: {cpu['temp']}C"
        if cpu.get("usage_pct"):
            cpu_line += f"  |  Usage: {cpu['usage_pct']}%"

    # System info
    sys_line = ""
    if system:
        parts = []
        if system.get("load_1m") is not None:
            parts.append(f"Load: {system['load_1m']}")
        if system.get("mem_used_mb") and system.get("mem_total_mb"):
            parts.append(f"RAM: {system['mem_used_mb']}/{system['mem_total_mb']}MB")
        if system.get("disk_used_pct") is not None:
            parts.append(f"Disk: {system['disk_used_pct']}%")
        sys_line = "  |  ".join(parts)

    # Compose
    lines = [subheader, "", gpu_table, "", miner_line, shares_line]
    if cpu_line:
        lines.extend(["", cpu_line])
    if sys_line:
        lines.append(sys_line)
    lines.append("\n[dim][B]ack  [L]ogs  [R]eboot  [E]xec  [O]C apply[/dim]")

    content = Text()
    for item in lines:
        if isinstance(item, str):
            content.append(item + "\n")

    # Build panel with mixed content (table + text)
    from rich.console import Group
    render_items = []
    for item in lines:
        if isinstance(item, Table):
            render_items.append(item)
        elif isinstance(item, str):
            render_items.append(Text.from_markup(item))

    return Panel(
        Group(*render_items),
        title=header,
        border_style="cyan",
        expand=True,
    )
