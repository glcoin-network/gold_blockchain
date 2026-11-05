"""Verify the exported deb and preserve disposable node data across reinstall."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sysconfig
import tarfile
import tempfile


PACKAGE = Path(os.environ.get("GOLD_DEB_PACKAGE", "/out/gold-blockchain-cli_2.0.5_amd64.deb"))
TEST_DATA = Path("/tmp/gold-runtime-smoke")
SITE = Path(sysconfig.get_paths()["purelib"])


def run(*arguments):
    return subprocess.check_output([str(value) for value in arguments], text=True).strip()


def snapshot():
    return {
        str(path.relative_to(TEST_DATA)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(TEST_DATA.rglob("*"))
        if path.is_file()
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=PACKAGE)
    parser.add_argument("--package-version", default="2.0.5")
    parser.add_argument("--cli-version", default="2.0.5")
    args = parser.parse_args()
    package = args.package
    metadata = {
        field: run("dpkg-deb", "--field", package, field)
        for field in ("Package", "Version", "Architecture", "Depends")
    }
    assert metadata["Package"] == "gold-blockchain-cli"
    assert metadata["Version"] == args.package_version
    assert metadata["Architecture"] == "amd64"
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["dpkg-deb", "--fsys-tarfile", str(package)], stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:") as payload:
            members = {
                member.name[2:] if member.name.startswith("./") else member.name: member
                for member in payload
            }
    required = [
        "usr/local/bin/gold",
        "opt/gold/gold_timelord",
        "opt/gold/gold_timelord_launcher",
        "opt/gold/gold_full_node",
        "opt/gold/gold_farmer",
        "opt/gold/gold_harvester",
        "opt/gold/gold_wallet",
        str((SITE / "vdf_client").relative_to("/")),
        str((SITE / "vdf_bench").relative_to("/")),
        "lib/systemd/system/gold-timelord-launcher@.service",
    ]
    for name in required:
        assert name in members, name
        if not name.endswith(".service"):
            assert members[name].mode & 0o111, name
    assert "usr/bin/gold" not in members, "The GNU Gold linker path must not be packaged"
    assert not any(name.startswith(("home/", "root/", "data/")) for name in members)
    dynamic_links = {}
    for filename in ("vdf_client", "vdf_bench"):
        output = run("ldd", SITE / filename)
        assert "not found" not in output, output
        dynamic_links[filename] = output.splitlines()
    system_gold_owner = run("dpkg-query", "-S", "/usr/bin/gold")
    assert system_gold_owner.split(":", 1)[0] in ("binutils", "binutils-gold"), system_gold_owner
    linker = Path("/usr/bin/gold")
    linker_hash = hashlib.sha256(linker.read_bytes()).hexdigest()
    assert linker_hash == Path("/out/binutils-gold-before.sha256").read_text().split()[0]
    linker_version = run(linker, "--version")
    assert linker_version.startswith("GNU gold"), linker_version
    version_before = run("/usr/local/bin/gold", "version")
    assert version_before == args.cli_version
    assert run("dpkg-query", "-W", "-f=${Version}", "gold-blockchain-cli") == args.package_version
    distribution_version = run(
        "/opt/gold/venv/bin/python", "-c",
        "from importlib.metadata import version; print(version('gold-blockchain'))",
    )
    assert distribution_version == args.cli_version
    build_info = json.loads(Path("/opt/gold/BUILD-INFO.json").read_text())
    assert build_info["version"] == args.package_version
    assert build_info["cli_version"] == args.cli_version
    stack_checks = {}
    if build_info.get("cleared_executable_stack_libraries"):
        for name in build_info["cleared_executable_stack_libraries"]:
            headers = run("readelf", "-lW", SITE / name)
            stack = [line.strip() for line in headers.splitlines() if "GNU_STACK" in line]
            assert stack and not any(" RWE " in f" {line} " for line in stack), (name, stack)
            stack_checks[name] = stack
    python_runtime = run(
        "/opt/gold/venv/bin/python", "-c",
        "import json, sys; print(json.dumps({'executable': sys.executable, 'base_prefix': sys.base_prefix}))",
    )
    if build_info.get("python_runtime") == "bundled":
        assert json.loads(python_runtime)["base_prefix"] == "/opt/gold/python", python_runtime
        assert "opt/gold/python/bin/python3.12" in members
    for name, expected in build_info["source_sha256"].items():
        assert hashlib.sha256((SITE / name).read_bytes()).hexdigest() == expected, name
    for name in ("vdf_client", "vdf_bench"):
        assert hashlib.sha256((SITE / name).read_bytes()).hexdigest() == build_info[f"{name}_sha256"], name
    assert run(
        "/usr/bin/env", "PATH=/usr/local/bin:/usr/bin:/bin", "gold", "version"
    ) == version_before
    before = snapshot()
    assert "mainnet/config/config.yaml" in before
    assert any(name.startswith("keys/") for name in before)
    overwrite = run("dpkg", "--install", package)
    assert snapshot() == before, "Installing over the existing deb modified disposable node data"
    assert hashlib.sha256(linker.read_bytes()).hexdigest() == linker_hash
    assert run("/usr/local/bin/gold", "version") == version_before
    removal = run("dpkg", "--remove", "gold-blockchain-cli")
    assert snapshot() == before, "Removing the deb modified disposable node data"
    assert hashlib.sha256(linker.read_bytes()).hexdigest() == linker_hash
    reinstall = run("dpkg", "--install", package)
    assert snapshot() == before, "Reinstalling the deb modified disposable node data"
    assert run("dpkg-query", "-S", linker) == system_gold_owner
    assert hashlib.sha256(linker.read_bytes()).hexdigest() == linker_hash
    assert run(linker, "--version") == linker_version
    assert run("/usr/local/bin/gold", "version") == version_before
    dependency_check = run("/opt/gold/venv/bin/python", "-m", "pip", "check")
    assert dependency_check == "No broken requirements found."
    checksum = hashlib.sha256(package.read_bytes()).hexdigest()
    expected_checksum = package.with_suffix(".deb.sha256").read_text().split()[0]
    assert checksum == expected_checksum
    result = {
        "metadata": metadata,
        "size_bytes": package.stat().st_size,
        "sha256": checksum,
        "required_payload_files": required,
        "native_dynamic_libraries": dynamic_links,
        "no_packaged_home_or_node_data": True,
        "disposable_data_files_preserved": len(before),
        "install_over_existing_package_preserves_data": True,
        "overwrite_installation_output": overwrite,
        "remove_and_reinstall": True,
        "removal_output": removal,
        "reinstall_output": reinstall,
        "cli_version": version_before,
        "python_distribution_version": distribution_version,
        "installed_source_hashes_match_build_info": True,
        "installed_native_hashes_match_build_info": True,
        "cli_path": "/usr/local/bin/gold",
        "system_gold_owner": system_gold_owner,
        "system_linker_sha256_preserved": linker_hash,
        "system_linker_version": linker_version.splitlines()[0],
        "no_packaged_system_linker": True,
        "cli_works_without_private_path": True,
        "dependency_check": dependency_check,
        "non_executable_stack_checks": stack_checks,
        "python_runtime": json.loads(python_runtime),
    }
    Path("/out/deb-installation-verification.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
