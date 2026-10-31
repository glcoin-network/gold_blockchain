"""Check service imports and a real native VDF proof without starting network services."""

from __future__ import annotations

import importlib
from importlib.metadata import version
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import chiavdf

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.timelord.timelord_launcher import find_vdf_client
from chia.types.blockchain_format.classgroup import ClassgroupElement
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.blockchain_format.vdf import VDFInfo, VDFProof, validate_vdf
from chia.util.ints import uint8, uint64


def read_exact(connection, length):
    result = bytearray()
    while len(result) < length:
        data = connection.recv(length - len(result))
        if not data:
            raise RuntimeError("VDF client closed the connection early")
        result.extend(data)
    return bytes(result)


def check_worker():
    worker = find_vdf_client()
    if not os.access(worker, os.X_OK):
        raise RuntimeError("VDF client is not executable")
    challenge = bytes32(bytes([17]) * 32)
    initial = ClassgroupElement.get_default_element()
    discriminant = str(chiavdf.create_discriminant(challenge, DEFAULT_CONSTANTS.DISCRIMINANT_SIZE_BITS))
    iterations = 1024
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(60)
        process = subprocess.Popen(
            [str(worker), "127.0.0.1", str(listener.getsockname()[1]), "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(60)
                connection.sendall(
                    b"T"
                    + f"{len(discriminant):03d}".encode()
                    + discriminant.encode()
                    + bytes([len(initial.data)])
                    + initial.data
                )
                if read_exact(connection, 2) != b"OK":
                    raise RuntimeError("VDF handshake failed")
                text = str(iterations)
                connection.sendall(f"{len(text):02d}{text}".encode())
                length = int.from_bytes(read_exact(connection, 4), "big")
                if not 0 < length < 1_000_000:
                    raise RuntimeError(f"Unexpected VDF proof length: {length}")
                proof = io.BytesIO(bytes.fromhex(read_exact(connection, length).decode()))
                actual_iters = int.from_bytes(proof.read(8), "big", signed=True)
                output_size = int.from_bytes(proof.read(8), "big", signed=True)
                output = ClassgroupElement.create(proof.read(output_size)[:ClassgroupElement.get_size()])
                witness = uint8(int.from_bytes(proof.read(1), "big", signed=True))
                vdf_info = VDFInfo(challenge, uint64(actual_iters), output)
                vdf_proof = VDFProof(witness, proof.read(), False)
                if actual_iters != iterations or not validate_vdf(vdf_proof, DEFAULT_CONSTANTS, initial, vdf_info):
                    raise RuntimeError("Native VDF proof validation failed")
                connection.sendall(b"010")
                if read_exact(connection, 4) != b"STOP":
                    raise RuntimeError("VDF stop handshake failed")
                connection.sendall(b"ACK")
            stdout, stderr = process.communicate(timeout=15)
            if process.returncode != 0:
                raise RuntimeError(f"VDF process exited {process.returncode}: {stderr.decode(errors='replace')}")
            return {"worker": str(worker), "proof_iterations": actual_iters, "valid": True, "exit_code": process.returncode}
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def main():
    packages = {name: version(name) for name in ("gold-blockchain", "gold_rs", "chia-rs", "chiavdf")}
    assert packages["gold_rs"] == "0.9.0"
    assert packages["chia-rs"] == "0.9.0"
    assert packages["chiavdf"] == "1.1.4"
    ca_mode = Path("mozilla-ca/cacert.pem").stat().st_mode & 0o777
    assert ca_mode == 0o644, f"Unexpected Mozilla CA permissions: {oct(ca_mode)}"
    services = [
        "chia.consensus.blockchain",
        "chia.full_node.full_node",
        "chia.farmer.farmer",
        "chia.harvester.harvester",
        "chia.timelord.timelord",
        "chia.timelord.timelord_api",
        "chia.wallet.wallet_node",
        "chia.server.start_full_node",
        "chia.server.start_farmer",
        "chia.server.start_harvester",
        "chia.server.start_timelord",
        "chia.server.start_wallet",
    ]
    for name in services:
        importlib.import_module(name)
    from chia.timelord.timelord_api import TimelordAPI
    from chia.util.api_decorators import get_metadata

    assert get_metadata(TimelordAPI.new_peak_timelord).peer_required
    result = {
        "python": sys.version,
        "packages": packages,
        "mozilla_ca_mode": oct(ca_mode),
        "service_imports": services,
        "slot_target": int(DEFAULT_CONSTANTS.SUB_SLOT_TIME_TARGET),
        "blocks_per_slot": int(DEFAULT_CONSTANTS.SLOT_BLOCKS_TARGET),
        "recovery_source_sha256": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in ("chia/timelord/timelord.py", "chia/timelord/timelord_api.py")
        },
        "vdf": check_worker(),
    }
    print(json.dumps(result, indent=2))
    Path("/opt/gold-image-verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
