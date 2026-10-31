"""Start service processes in a disposable, network-isolated container."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time

from chia.daemon.client import connect_to_daemon_and_validate
from chia.util.config import load_config, save_config
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.keychain import Keychain, generate_mnemonic


SERVICES = [
    "gold_full_node",
    "gold_wallet",
    "gold_farmer",
    "gold_harvester",
    "gold_timelord",
    "gold_timelord_launcher",
]


async def verify_services():
    Keychain().add_key(generate_mnemonic())
    subprocess.run(["gold", "init"], check=True, timeout=60)
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
    for section in ["logging"] + SERVICES:
        settings = config.get(section.replace("gold_", "", 1), {})
        settings.get("logging", settings)["log_level"] = "INFO"
    config["full_node"]["dns_servers"] = []
    config["full_node"]["introducer_peer"] = None
    config["full_node"]["target_outbound_peer_count"] = 0
    config["full_node"]["reserved_cores"] = max(0, (os.cpu_count() or 2) - 2)
    config["wallet"]["connect_to_unknown_peers"] = False
    save_config(DEFAULT_ROOT_PATH, "config.yaml", config)
    with tempfile.TemporaryFile(mode="w+") as output:
        process = subprocess.Popen(["gold", "run_daemon"], stdout=output, stderr=output)
        daemon = None
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Daemon exited early: {process.returncode}")
                daemon = await connect_to_daemon_and_validate(DEFAULT_ROOT_PATH, config, quiet=True)
                if daemon is not None:
                    break
                await asyncio.sleep(1)
            if daemon is None:
                raise RuntimeError("Daemon did not become ready")
            for service in SERVICES:
                response = await daemon.start_service(service_name=service)
                if not response.get("data", {}).get("success"):
                    raise RuntimeError(f"Service launch failed for {service}: {response}")
            await asyncio.sleep(20)
            status = {service: await daemon.is_running(service_name=service) for service in SERVICES}
            result = {"network": "none", "host_node_data_mounted": False, "processes_running": status}
            debug_log = DEFAULT_ROOT_PATH / "log/debug.log"
            if debug_log.exists():
                text = debug_log.read_text(errors="replace")
                result["finished_pot_records"] = text.count("Finished PoT")
                result["timelord_connected_to_full_node"] = any(
                    "timelord" in line and "Connected with full_node" in line for line in text.splitlines()
                )
                failures = [
                    line for line in text.splitlines()
                    if "Traceback (most recent call last)" in line or "Invalid proof of time!" in line
                ]
                result["runtime_failures"] = failures
            else:
                result["runtime_failures"] = ["No runtime debug log"]
            print(json.dumps(result, indent=2), flush=True)
            if not all(status.values()) or result["runtime_failures"]:
                if debug_log.exists():
                    print(debug_log.read_text(errors="replace")[-24000:], flush=True)
                raise RuntimeError("Isolated service startup verification failed")
        finally:
            if daemon is not None:
                try:
                    await daemon.exit()
                finally:
                    await daemon.close()
            if process.poll() is None:
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
            output.seek(0)
            daemon_output = output.read()
            print(daemon_output[-6000:], flush=True)
            if "UNPROTECTED SSL FILE" in daemon_output:
                raise RuntimeError("Image CA permissions triggered an SSL warning")


if __name__ == "__main__":
    asyncio.run(verify_services())
