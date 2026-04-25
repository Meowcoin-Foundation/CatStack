"""CatStack CLI entry point (console build).

Forwards to the click CLI. Run as ``CatStackCLI.exe rig list`` / ``status`` /
``--help``.
"""
from mfarm.cli import cli


if __name__ == "__main__":
    cli()
