from __future__ import annotations

import json
import os
from pathlib import Path

import click
from rich.console import Console

from mfarm.db.connection import get_db
from mfarm.db.models import Rig
from mfarm.targets import resolve_targets

console = Console()

# Worker files are bundled in the package
WORKER_DIR = Path(__file__).parent.parent / "worker"


@click.group("deploy")
def deploy_group():
    """Deploy agent and miner software to rigs."""
    pass


@deploy_group.command("agent")
@click.argument("target")
@click.option("--disable-hiveos", is_flag=True, help="Disable HiveOS agent on the rig")
def deploy_agent(target, disable_hiveos):
    """Deploy MFarm agent to rig(s). TARGET can be rig name, group:name, or all."""
    from mfarm.ssh.pool import get_pool

    db = get_db()
    rigs = resolve_targets(db, target)
    pool = get_pool()

    agent_file = WORKER_DIR / "mfarm-agent.py"
    deploy_script = WORKER_DIR / "deploy.sh"
    service_file = WORKER_DIR / "mfarm-agent.service"
    wrapper_file = WORKER_DIR / "miner-wrapper.sh"

    for f in [agent_file, deploy_script, service_file]:
        if not f.exists():
            raise click.ClickException(f"Missing worker file: {f}")

    console.print(f"[bold]Deploying agent to {len(rigs)} rig(s)...[/bold]\n")

    for rig in rigs:
        console.print(f"[cyan]{rig.name}[/cyan] ({rig.host}):")
        try:
            # Create staging dir on rig
            pool.exec(rig, "mkdir -p /tmp/mfarm-deploy")

            # Upload files
            console.print("  Uploading agent files...")
            pool.upload(rig, str(agent_file), "/tmp/mfarm-deploy/mfarm-agent.py")
            pool.upload(rig, str(deploy_script), "/tmp/mfarm-deploy/deploy.sh")
            pool.upload(rig, str(service_file), "/tmp/mfarm-deploy/mfarm-agent.service")
            if wrapper_file.exists():
                pool.upload(rig, str(wrapper_file), "/tmp/mfarm-deploy/miner-wrapper.sh")

            # Run deploy script
            env_prefix = ""
            if disable_hiveos:
                env_prefix = "DISABLE_HIVEOS=1 "

            console.print("  Running deploy script...")
            stdout, stderr, rc = pool.exec(
                rig, f"{env_prefix}bash /tmp/mfarm-deploy/deploy.sh", timeout=120
            )
            if stdout:
                for line in stdout.strip().split("\n"):
                    console.print(f"  {line}")
            if rc != 0:
                console.print(f"  [red]Deploy failed (exit code {rc})[/red]")
                if stderr:
                    console.print(f"  [red]{stderr.strip()}[/red]")
                continue

            # Read hardware info back and update DB
            hw_out, _, hw_rc = pool.exec(rig, "cat /var/run/mfarm/hwinfo.json", timeout=5)
            if hw_rc == 0 and hw_out.strip():
                try:
                    hw = json.loads(hw_out)
                    gpus = hw.get("gpus", [])
                    rig.gpu_list = json.dumps([g.get("name", "Unknown") for g in gpus])
                    rig.cpu_model = hw.get("cpu_model")
                    rig.os_info = hw.get("os", "")
                    if hw.get("hiveos_version"):
                        rig.os_info += f" (HiveOS {hw['hiveos_version']})"
                    rig.agent_version = "0.1.0"
                    rig.save(db)
                    console.print(f"  [green]Updated rig info: {len(gpus)} GPU(s), {rig.cpu_model or 'unknown CPU'}[/green]")
                except json.JSONDecodeError:
                    pass

            console.print(f"  [green]Deploy complete[/green]")

        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")

        console.print()

    console.print("[bold green]Done.[/bold green]")


@deploy_group.command("miner")
@click.argument("miner_name")
@click.argument("target")
@click.option("--binary", type=click.Path(exists=True), help="Local path to miner binary/archive")
@click.option("--url", help="URL to download miner from on the rig")
def deploy_miner(miner_name, target, binary, url):
    """Deploy miner software to rig(s)."""
    from mfarm.ssh.pool import get_pool

    if not binary and not url:
        raise click.ClickException("Provide --binary (local file to upload) or --url (download on rig)")

    db = get_db()
    rigs = resolve_targets(db, target)
    pool = get_pool()

    console.print(f"[bold]Deploying {miner_name} to {len(rigs)} rig(s)...[/bold]\n")

    for rig in rigs:
        console.print(f"[cyan]{rig.name}[/cyan] ({rig.host}):")
        try:
            dest_dir = f"/opt/mfarm/miners"
            pool.exec(rig, f"mkdir -p {dest_dir}")

            if binary:
                filename = os.path.basename(binary)
                remote_path = f"/tmp/{filename}"

                console.print(f"  Uploading {filename}...")
                pool.upload(rig, binary, remote_path)

                # Detect archive type and extract, or just copy binary
                if filename.endswith((".tar.gz", ".tgz")):
                    pool.exec(rig, f"tar xzf {remote_path} -C {dest_dir}")
                elif filename.endswith(".tar.xz"):
                    pool.exec(rig, f"tar xJf {remote_path} -C {dest_dir}")
                elif filename.endswith(".zip"):
                    pool.exec(rig, f"unzip -o {remote_path} -d {dest_dir}")
                else:
                    pool.exec(rig, f"cp {remote_path} {dest_dir}/{miner_name} && chmod +x {dest_dir}/{miner_name}")

                pool.exec(rig, f"rm -f {remote_path}")

            elif url:
                console.print(f"  Downloading from URL...")
                filename = url.split("/")[-1]
                stdout, stderr, rc = pool.exec(
                    rig, f"wget -q -O /tmp/{filename} '{url}'", timeout=120
                )
                if rc != 0:
                    console.print(f"  [red]Download failed: {stderr.strip()}[/red]")
                    continue

                if filename.endswith((".tar.gz", ".tgz")):
                    pool.exec(rig, f"tar xzf /tmp/{filename} -C {dest_dir}")
                elif filename.endswith(".tar.xz"):
                    pool.exec(rig, f"tar xJf /tmp/{filename} -C {dest_dir}")
                elif filename.endswith(".zip"):
                    pool.exec(rig, f"unzip -o /tmp/{filename} -d {dest_dir}")

                pool.exec(rig, f"rm -f /tmp/{filename}")

            # Verify
            check_cmd = f"ls -la {dest_dir}/"
            stdout, _, _ = pool.exec(rig, check_cmd, timeout=5)
            console.print(f"  Files in {dest_dir}/:")
            for line in stdout.strip().split("\n")[-5:]:  # Last 5 lines
                console.print(f"    {line}")

            console.print(f"  [green]Deploy complete[/green]")

        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")

        console.print()

    console.print("[bold green]Done.[/bold green]")
