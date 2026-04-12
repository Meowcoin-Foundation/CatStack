import click


@click.command("web")
@click.option("--host", default="0.0.0.0", help="Bind address")
@click.option("--port", default=8080, help="Port number")
def web_cmd(host, port):
    """Launch the web dashboard."""
    from rich.console import Console
    console = Console()
    console.print(f"[bold cyan]MeowFarm Web Dashboard[/bold cyan]")
    console.print(f"  Local:   http://127.0.0.1:{port}")
    console.print(f"  Network: http://0.0.0.0:{port}")
    console.print(f"  [dim]Press Ctrl+C to stop[/dim]\n")

    from mfarm.web.app import run_server
    run_server(host=host, port=port)
