"""Formatting utilities for dashboard display."""

from __future__ import annotations


def format_hashrate(h: float | None) -> str:
    if h is None or h == 0:
        return "-"
    if h >= 1e12:
        return f"{h/1e12:.2f} TH/s"
    if h >= 1e9:
        return f"{h/1e9:.2f} GH/s"
    if h >= 1e6:
        return f"{h/1e6:.2f} MH/s"
    if h >= 1e3:
        return f"{h/1e3:.2f} KH/s"
    return f"{h:.1f} H/s"


def format_uptime(secs: int | None) -> str:
    if secs is None or secs == 0:
        return "-"
    days = secs // 86400
    hours = (secs % 86400) // 3600
    mins = (secs % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def format_power(watts: float | None) -> str:
    if watts is None:
        return "-"
    return f"{watts:.0f}W"


def format_temp(temp: int | None) -> str:
    if temp is None:
        return "-"
    return f"{temp}C"


def temp_color(temp: int | None) -> str:
    if temp is None:
        return "dim"
    if temp >= 80:
        return "red bold"
    if temp >= 65:
        return "yellow"
    return "green"


def format_temps(gpus: list[dict]) -> str:
    if not gpus:
        return "-"
    temps = [g.get("temp") for g in gpus if g.get("temp") is not None]
    if not temps:
        return "-"
    return " ".join(str(t) for t in temps)


def format_temps_colored(gpus: list[dict]) -> str:
    if not gpus:
        return "-"
    parts = []
    for g in gpus:
        t = g.get("temp")
        if t is None:
            parts.append("[dim]-[/dim]")
        else:
            color = temp_color(t)
            parts.append(f"[{color}]{t}[/{color}]")
    return " ".join(parts)


def format_fans(gpus: list[dict]) -> str:
    if not gpus:
        return "-"
    fans = [g.get("fan") for g in gpus if g.get("fan") is not None]
    if not fans:
        return "-"
    return " ".join(f"{f}%" for f in fans)


def total_power(gpus: list[dict]) -> float:
    return sum(g.get("power_draw", 0) or 0 for g in gpus)


def status_icon(stats: dict | None, error: Exception | None) -> str:
    if error:
        return "[red]DOWN[/red]"
    if stats is None:
        return "[blue]WAIT[/blue]"

    miner = stats.get("miner", {})
    if not miner.get("running"):
        return "[yellow]IDLE[/yellow]"

    # Check for warnings
    gpus = stats.get("gpus", [])
    max_temp = max((g.get("temp", 0) or 0 for g in gpus), default=0)
    if max_temp >= 80:
        return "[red]HOT![/red]"
    if miner.get("hashrate", 0) == 0:
        return "[yellow]WARN[/yellow]"

    return "[green] OK [/green]"


def share_ratio(stats: dict) -> str:
    miner = stats.get("miner", {})
    acc = miner.get("accepted", 0)
    rej = miner.get("rejected", 0)
    total = acc + rej
    if total == 0:
        return "-"
    pct = rej / total * 100 if total > 0 else 0
    return f"A:{acc} R:{rej} ({pct:.1f}%)"
