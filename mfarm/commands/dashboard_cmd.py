import click

from mfarm.config import DASHBOARD_REFRESH_INTERVAL


@click.command("dashboard")
@click.option("--group", "group_filter", default=None, help="Show only rigs in this group")
@click.option("--refresh", default=DASHBOARD_REFRESH_INTERVAL, help="Refresh interval in seconds")
def dashboard_cmd(group_filter, refresh):
    """Launch the live mining dashboard."""
    from mfarm.dashboard.app import Dashboard

    dashboard = Dashboard(group_filter=group_filter, refresh_interval=refresh)
    dashboard.run()
