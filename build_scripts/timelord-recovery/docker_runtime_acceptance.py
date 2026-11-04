"""Exercise installed services and native workers in a disposable offline container."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import traceback

from gold_rs import AugSchemeMPL

from chia.daemon.client import connect_to_daemon_and_validate
from chia.rpc.farmer_rpc_client import FarmerRpcClient
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.rpc.harvester_rpc_client import HarvesterRpcClient
from chia.rpc.rpc_client import RpcClient
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.server.outbound_message import NodeType
from chia.util.config import load_config, save_config
from chia.util.default_root import DEFAULT_KEYS_ROOT_PATH, DEFAULT_ROOT_PATH
from chia.util.ints import uint16
from chia.util.keychain import Keychain, generate_mnemonic


RESULTS = Path("/results")
SERVICES = [
    "gold_full_node", "gold_wallet", "gold_farmer",
    "gold_harvester", "gold_timelord", "gold_timelord_launcher",
]
CLIENTS = {
    "full_node": (FullNodeRpcClient, 14555),
    "wallet": (WalletRpcClient, 14256),
    "farmer": (FarmerRpcClient, 14559),
    "harvester": (HarvesterRpcClient, 14560),
    "timelord": (RpcClient, 14557),
}


def write_result(result):
    (RESULTS / "runtime-acceptance.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )


def log(message):
    print(message, flush=True)


def unexpected_runtime_errors(records, stop_windows):
    windows = [
        (datetime.fromisoformat(window["started_at_utc"]), datetime.fromisoformat(window["finished_at_utc"]))
        for window in stop_windows if "finished_at_utc" in window
    ]
    unexpected = []
    for line in records:
        match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+) ", line)
        stamp = None if match is None else datetime.fromisoformat(match[1]).replace(tzinfo=timezone.utc)
        in_stop_window = stamp is not None and any(start <= stamp <= finish for start, finish in windows)
        if not (
            in_stop_window and "chia.timelord.timelord_launcher" in line
            and "connect: Connection refused" in line
        ):
            unexpected.append(line)
    return unexpected


def keyring_hashes():
    return {
        str(path.relative_to(DEFAULT_KEYS_ROOT_PATH)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in DEFAULT_KEYS_ROOT_PATH.rglob("*.yaml")
    }


def sqlite_checks():
    checks = {}
    for path in DEFAULT_ROOT_PATH.rglob("*.sqlite"):
        with sqlite3.connect(str(path)) as database:
            rows = database.execute("PRAGMA quick_check").fetchall()
            assert rows == [("ok",)], (path, rows)
        checks[str(path.relative_to(DEFAULT_ROOT_PATH))] = "ok"
    assert any(name.startswith("db/") for name in checks)
    assert any(name.startswith("wallet/") for name in checks)
    return checks


async def wait_for_health(clients):
    deadline = time.monotonic() + 90
    last_error = None
    while time.monotonic() < deadline:
        try:
            for client in clients.values():
                response = await asyncio.wait_for(client.healthz(), timeout=5)
                assert response["success"], response
            return
        except Exception as error:
            last_error = error
            await asyncio.sleep(2)
    raise RuntimeError(f"RPC readiness failed: {last_error!r}")


async def wait_for_connections(clients):
    deadline = time.monotonic() + 60
    last_error = None
    while time.monotonic() < deadline:
        try:
            for node_type in (NodeType.TIMELORD, NodeType.FARMER, NodeType.WALLET):
                assert await clients["full_node"].get_connections(node_type), node_type
            assert await clients["farmer"].get_connections(NodeType.HARVESTER)
            return
        except Exception as error:
            last_error = error
            await asyncio.sleep(1)
    raise RuntimeError(f"Local peer readiness failed: {last_error!r}")


async def start_services(daemon):
    for service in SERVICES:
        response = await daemon.start_service(service)
        assert response.get("data", {}).get("success"), (service, response)


async def stop_services(daemon, result, reason):
    window = {"reason": reason, "started_at_utc": datetime.now(timezone.utc).isoformat()}
    result.setdefault("service_stop_windows_utc", []).append(window)
    # Quiesce timelord before terminating the native workers it communicates with.
    for service in (
        "gold_timelord", "gold_timelord_launcher", "gold_harvester",
        "gold_farmer", "gold_wallet", "gold_full_node",
    ):
        response = await daemon.stop_service(service)
        assert response.get("data", {}).get("success"), (service, response)
    for service in SERVICES:
        assert not await daemon.is_running(service), service
    window["finished_at_utc"] = datetime.now(timezone.utc).isoformat()


async def sample_services(daemon, clients, fingerprint):
    running = {service: await daemon.is_running(service) for service in SERVICES}
    assert all(running.values()), running
    for name, client in clients.items():
        response = await asyncio.wait_for(client.healthz(), timeout=10)
        assert response["success"], name
    state = await clients["full_node"].get_blockchain_state()
    connections = await clients["full_node"].get_connections(NodeType.TIMELORD)
    assert connections, "Full node has no local timelord connection"
    farmer_connections = await clients["full_node"].get_connections(NodeType.FARMER)
    wallet_connections = await clients["full_node"].get_connections(NodeType.WALLET)
    harvester_connections = await clients["farmer"].get_connections(NodeType.HARVESTER)
    assert farmer_connections and wallet_connections and harvester_connections
    fingerprints = await clients["wallet"].get_public_keys()
    assert fingerprint in fingerprints, "Wallet cannot read the disposable key"
    wallets = await clients["wallet"].get_wallets()
    assert wallets, "Wallet database did not initialize"
    balance = await clients["wallet"].get_wallet_balance(wallets[0]["id"])
    plots = await clients["harvester"].get_plots()
    harvesters = await clients["farmer"].get_harvesters()
    return {
        "services_running": running,
        "rpc_health": {name: True for name in clients},
        "timelord_connections": len(connections),
        "farmer_full_node_connections": len(farmer_connections),
        "wallet_full_node_connections": len(wallet_connections),
        "harvester_farmer_connections": len(harvester_connections),
        "wallet_count": len(wallets),
        "wallet_confirmed_balance": balance["confirmed_wallet_balance"],
        "plot_count": len(plots["plots"]),
        "connected_harvesters": len(harvesters["harvesters"]),
        "chain_peak_present": state["peak"] is not None,
    }


async def run_acceptance(result):
    spec = importlib.util.spec_from_file_location("native_smoke", "/checks/smoke_test.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    result["native_worker_cycles"] = []
    for index in range(10):
        proof = native.check_worker()
        result["native_worker_cycles"].append(proof)
        write_result(result)
        log(f"Native VDF proof/start/stop cycle {index + 1}/10 passed")
    key = Keychain().add_key(generate_mnemonic())
    fingerprint = key.get_g1().get_fingerprint()
    message = b"gold-disposable-docker-acceptance"
    signature = AugSchemeMPL.sign(key, message)
    assert AugSchemeMPL.verify(key.get_g1(), message, signature)
    assert not AugSchemeMPL.verify(key.get_g1(), message + b"-altered", signature)
    result["bls_sign_verify_and_reject_altered_message"] = True
    subprocess.run(["gold", "init"], check=True, timeout=60)
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
    config["logging"]["log_level"] = "INFO"
    config["full_node"]["dns_servers"] = []
    config["full_node"]["introducer_peer"] = None
    config["full_node"]["target_outbound_peer_count"] = 0
    config["full_node"]["reserved_cores"] = max(0, (os.cpu_count() or 2) - 2)
    config["wallet"]["connect_to_unknown_peers"] = False
    config["wallet"]["dns_servers"] = []
    config["wallet"]["introducer_peer"] = None
    config["rpc_timeout"] = 15
    save_config(DEFAULT_ROOT_PATH, "config.yaml", config)
    before_keys = keyring_hashes()
    assert before_keys
    clients = {}
    daemon = None
    with (RESULTS / "daemon-output.txt").open("w+") as output:
        process = subprocess.Popen(["gold", "run_daemon"], stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert process.poll() is None, "Daemon exited during startup"
                daemon = await connect_to_daemon_and_validate(DEFAULT_ROOT_PATH, config, quiet=True)
                if daemon is not None:
                    break
                await asyncio.sleep(1)
            assert daemon is not None, "Daemon did not become ready"
            await start_services(daemon)
            for name, (client_class, default_port) in CLIENTS.items():
                clients[name] = await client_class.create(
                    "127.0.0.1", uint16(config[name].get("rpc_port", default_port)),
                    DEFAULT_ROOT_PATH, config,
                )
            await wait_for_health(clients)
            public_keys = await clients["wallet"].get_public_keys()
            assert fingerprint in public_keys
            login = await clients["wallet"].log_in(fingerprint)
            assert isinstance(login, dict) and login.get("success"), login
            await wait_for_connections(clients)
            wallets = await clients["wallet"].get_wallets()
            assert wallets
            address = await clients["wallet"].get_next_address(wallets[0]["id"], True)
            assert address.startswith("gl1")
            result["wallet_login_and_address_derivation"] = True
            result["samples"] = []
            result["sqlite_checks_during_restart"] = None
            started = time.monotonic()
            duration = int(os.environ.get("GOLD_ACCEPTANCE_SECONDS", "480"))
            restarted = False
            while time.monotonic() - started < duration:
                elapsed = time.monotonic() - started
                if elapsed >= duration / 2 and not restarted:
                    log("Stopping all six services and checking SQLite before restart")
                    await stop_services(daemon, result, "planned_restart")
                    result["sqlite_checks_during_restart"] = sqlite_checks()
                    assert keyring_hashes() == before_keys
                    await start_services(daemon)
                    await wait_for_health(clients)
                    login = await clients["wallet"].log_in(fingerprint)
                    assert isinstance(login, dict) and login.get("success"), login
                    await wait_for_connections(clients)
                    restored_address = await clients["wallet"].get_next_address(wallets[0]["id"], False)
                    assert restored_address == address, "Wallet address changed after restart"
                    result["wallet_address_preserved_after_restart"] = True
                    restarted = True
                    result["all_six_services_stopped_and_restarted"] = True
                sample = await sample_services(daemon, clients, fingerprint)
                sample["elapsed_seconds"] = round(time.monotonic() - started, 2)
                result["samples"].append(sample)
                write_result(result)
                log(f"Runtime {sample['elapsed_seconds']:.0f}s: five RPC services healthy; six processes running")
                await asyncio.sleep(min(30, max(0, duration - (time.monotonic() - started))))
            result["observed_runtime_seconds"] = round(time.monotonic() - started, 2)
            result["final_sample"] = await sample_services(daemon, clients, fingerprint)
            await stop_services(daemon, result, "final_shutdown")
            result["sqlite_checks_after_stop"] = sqlite_checks()
            assert keyring_hashes() == before_keys
            result["disposable_keyring_unchanged"] = True
        finally:
            for client in clients.values():
                client.close()
                await client.await_closed()
            if daemon is not None:
                try:
                    await daemon.exit()
                finally:
                    await daemon.close()
            if process.poll() is None:
                try:
                    process.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
    text = (DEFAULT_ROOT_PATH / "log/debug.log").read_text(errors="replace")
    result["finished_pot_records"] = text.count("Finished PoT")
    result["finished_signage_point_records"] = text.count("Finished signage point")
    assert result["finished_pot_records"] > 0, "No integrated PoT calculation observed"
    assert result["finished_signage_point_records"] > 0, "Full node received no signage points"
    result["error_log_records"] = [
        line for line in text.splitlines() if re.search(r":\s+ERROR(?:\s|$)", line)
    ]
    result["unexpected_error_log_records"] = unexpected_runtime_errors(
        result["error_log_records"], result.get("service_stop_windows_utc", [])
    )
    failures = [
        line for line in text.splitlines()
        if any(pattern in line for pattern in (
            "Traceback (most recent call last)", "Invalid proof of time!",
            "pure virtual method called", "unaligned tcache", "Vdf clients failed",
        ))
    ]
    daemon_text = (RESULTS / "daemon-output.txt").read_text(errors="replace")
    failures.extend(
        line for line in daemon_text.splitlines()
        if any(pattern in line for pattern in (
            "Traceback (most recent call last)", "pure virtual method called",
            "unaligned tcache", "UNPROTECTED SSL FILE",
        ))
    )
    failures.extend(result["unexpected_error_log_records"])
    result["critical_log_records"] = failures
    assert not failures, failures


def main():
    RESULTS.mkdir(exist_ok=True)
    result = {
        "status": "running", "network": "none", "host_node_data_mounted": False,
        "production_chain_database_tested": False, "mainnet_block_production_tested": False,
        "real_stake_transaction_tested": False, "real_wallet_transaction_tested": False,
        "systemd_lifecycle_tested": False,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_runtime_seconds": int(os.environ.get("GOLD_ACCEPTANCE_SECONDS", "480")),
    }
    try:
        asyncio.run(run_acceptance(result))
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["error"] = repr(error)
        traceback.print_exc()
        raise
    finally:
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        debug_log = DEFAULT_ROOT_PATH / "log/debug.log"
        if debug_log.exists():
            (RESULTS / "debug.log").write_text(
                debug_log.read_text(errors="replace"), encoding="utf-8"
            )
        write_result(result)
        log(json.dumps({key: value for key, value in result.items() if key != "samples"}, indent=2))


if __name__ == "__main__":
    main()
