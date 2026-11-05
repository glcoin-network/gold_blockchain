"""Assemble a CLI deb with the tested Python dependencies and native VDF worker."""

from __future__ import annotations

import argparse
import ast
import configparser
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


TARGETS = {
    "universal": {
        "python": "3.8",
        "bundled_python": True,
        "depends": "libc6 (>= 2.31), libstdc++6 (>= 10), libgcc-s1, ca-certificates, netcat-openbsd",
    },
    "20.04": {
        "python": "3.8",
        "depends": (
            "python3.8 (>= 3.8.1), python3.8 (<< 3.9), libpython3.8-stdlib, python3-distutils, "
            "libc6 (>= 2.31), libstdc++6 (>= 9), libgcc-s1, libgmp10, libgmpxx4ldbl, "
            "libssl1.1, libffi7, ca-certificates, netcat-openbsd"
        ),
    },
    "24.04": {
        "python": "3.12",
        "depends": (
            "python3.12 (>= 3.12), python3.12 (<< 3.13), libpython3.12-stdlib, "
            "libc6 (>= 2.39), libstdc++6 (>= 13), libgcc-s1, libgmp10, libgmpxx4ldbl, "
            "libssl3t64, libffi8, ca-certificates, netcat-openbsd"
        ),
    },
    "26.04": {
        "python": "3.12",
        "bundled_python": True,
        "depends": (
            "libc6 (>= 2.43), libstdc++6 (>= 16), libgcc-s1, libgmp10, libgmpxx4ldbl, "
            "libssl3t64, libffi8, libexpat1, libbz2-1.0, liblzma5, libsqlite3-0, "
            "zlib1g, libreadline8t64, libncursesw6, libtinfo6, libuuid1, libcrypt1, "
            "libgdbm6t64, libgdbm-compat4t64, ca-certificates, netcat-openbsd"
        ),
    },
}


def run(*arguments):
    subprocess.run([str(value) for value in arguments], check=True)


def write_file(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def clear_incompatible_executable_stacks(site):
    """Clear malformed executable-stack declarations from native extensions."""
    patched = []
    for path in sorted(site.rglob("*.so*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            headers = subprocess.check_output(["readelf", "-lW", path], text=True)
        except (OSError, subprocess.CalledProcessError):
            continue
        stack_headers = [line for line in headers.splitlines() if "GNU_STACK" in line]
        if not any(" RWE " in f" {line} " for line in stack_headers):
            continue
        if path.parent.name != "chiapos.libs" or not path.name.startswith("libbladebit_harvester-"):
            raise RuntimeError(f"Unexpected executable-stack library requires investigation: {path}")
        run("patchelf", "--clear-execstack", path)
        updated_headers = subprocess.check_output(["readelf", "-lW", path], text=True)
        if any(" RWE " in f" {line} " for line in updated_headers.splitlines() if "GNU_STACK" in line):
            raise RuntimeError(f"Failed to clear executable stack declaration: {path}")
        patched.append(str(path.relative_to(site)))
    return patched


def set_release_version(setup_path, version):
    source = setup_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    kwargs = next(
        node.value for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "kwargs" for target in node.targets)
    )
    value = next(keyword.value for keyword in kwargs.keywords if keyword.arg == "version")
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or value.lineno != value.end_lineno:
        raise RuntimeError("Unexpected setup.py version definition")
    old_version = value.value
    lines = source.splitlines(keepends=True)
    line = lines[value.lineno - 1]
    lines[value.lineno - 1] = line[:value.col_offset] + repr(version) + line[value.end_col_offset:]
    setup_path.write_text("".join(lines), encoding="utf-8")
    return old_version


def copy_runtime(source, stage, wheel, python_version, bundled_python=None, portable=False):
    runtime = stage / "opt/gold/venv"
    shutil.copytree("/opt/venv", runtime, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    site = runtime / f"lib/python{python_version}/site-packages"
    for path in list(site.glob("__editable__*gold_blockchain*")) + list(site.glob("gold_blockchain-*.dist-info")):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    run("/opt/venv/bin/python", "-m", "pip", "install", "--no-index", "--no-deps", "--upgrade", "--target", site, wheel)
    generated_bin = site / "bin"
    if generated_bin.exists():
        shutil.rmtree(generated_bin)
    patched_execstack = clear_incompatible_executable_stacks(site) if bundled_python is not None and not portable else []
    python3 = runtime / "bin/python3"
    python3.unlink()
    if bundled_python is None:
        python3.symlink_to(f"/usr/bin/python{python_version}")
    else:
        private_python = Path("/opt/gold/python")
        shutil.copytree(
            bundled_python, stage / private_python.relative_to("/"),
            symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        python3.symlink_to(private_python / f"bin/python{python_version}")
        for name in ("python", f"python{python_version}"):
            link = runtime / "bin" / name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to("python3")
        write_file(
            runtime / "pyvenv.cfg",
            f"home = {private_python}/bin\n"
            "include-system-site-packages = false\n"
            f"version = {sys.version.split()[0]}\n"
            f"executable = {private_python}/bin/python{python_version}\n",
        )
    for path in (runtime / "bin").iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if data.startswith(b"#!/opt/venv/bin/python"):
            _, _, body = data.partition(b"\n")
            path.write_bytes(b"#!/opt/gold/venv/bin/python\n" + body)
    for filename in ("cacert.pem",):
        target = site / "mozilla-ca" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "mozilla-ca" / filename, target)
        target.chmod(0o644)
    for name in ("test_peak_recovery.py", "test_iteration_consistency.py", "test_candidate_refresh.py"):
        test = Path("chia/_tests/timelord") / name
        (site / test).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / test, site / test)
    for path in (runtime / "bin").iterdir():
        if not path.name.startswith("gold") or not path.is_file():
            continue
        write_file(
            stage / "opt/gold" / path.name,
            "#!/bin/sh\n"
            'export PATH="/opt/gold/venv/bin:$PATH"\n'
            "export PYTHONNOUSERSITE=1\n"
            f'exec /opt/gold/venv/bin/{path.name} "$@"\n',
            0o755,
        )
    # Ubuntu's binutils owns /usr/bin/gold (the GNU Gold linker).
    # Keep the Gold blockchain CLI in the traditional /usr/local/bin path.
    user_bin = stage / "usr/local/bin"
    user_bin.mkdir(parents=True, exist_ok=True)
    (user_bin / "gold").symlink_to("/opt/gold/gold")
    if portable:
        from portable_runtime import prepare_portable_runtime

        patched_execstack = prepare_portable_runtime(stage)
    return site, patched_execstack


def copy_units(source, stage):
    destination = stage / "lib/systemd/system"
    destination.mkdir(parents=True, exist_ok=True)
    for path in (source / "build_scripts/assets/systemd").glob("*.service"):
        shutil.copyfile(path, destination / path.name)
    timelord_unit = destination / "gold-timelord@.service"
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(timelord_unit)
    parser["Unit"]["Wants"] = "gold-timelord-launcher@%i.service"
    with timelord_unit.open("w") as stream:
        parser.write(stream, space_around_delimiters=False)
    write_file(
        destination / "gold-timelord-launcher@.service",
        "[Unit]\n"
        "Description=Gold VDF Worker Launcher for %i\n"
        "Requires=gold-timelord@%i.service\n"
        "After=gold-timelord@%i.service\n\n"
        "[Service]\n"
        "Type=simple\n"
        "Environment=GOLD_ROOT=/home/%i/.gold/mainnet\n"
        "ExecStart=/opt/gold/gold_timelord_launcher\n"
        "User=%i\nGroup=%i\n"
        "LimitNOFILE=1048576\nLimitNPROC=1048576\n\n"
        "[Install]\nWantedBy=multi-user.target\n",
    )
    for path in destination.iterdir():
        path.chmod(0o644)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="2.0.5")
    parser.add_argument("--revision", help="Optional numeric Debian revision; omit for a version such as 2.0.5")
    parser.add_argument("--base-image-id", required=True)
    parser.add_argument("--source-revision", default="unknown")
    parser.add_argument("--native-revision", default="unknown")
    parser.add_argument("--target-ubuntu", choices=TARGETS, default="20.04")
    parser.add_argument("--bundled-python", type=Path, help="Private interpreter and standard library prefix")
    parser.add_argument("--include-ubuntu-in-filename", action="store_true")
    parser.add_argument("--filename-suffix", help="Explicit suffix after the amd64 architecture")
    args = parser.parse_args()
    target = TARGETS[args.target_ubuntu]
    if target.get("bundled_python", False) != (args.bundled_python is not None):
        raise RuntimeError(f"Ubuntu {args.target_ubuntu} bundled interpreter setting does not match its target")
    python_version = ".".join(str(part) for part in sys.version_info[:2])
    if python_version != target["python"]:
        raise RuntimeError(f"Ubuntu {args.target_ubuntu} requires Python {target['python']}, got {python_version}")
    if not re.fullmatch(r"\d+(?:\.\d+)*", args.version) or (
        args.revision is not None and not re.fullmatch(r"\d+", args.revision)
    ):
        raise RuntimeError("Release version and revision must be numeric")
    debian_version = f"{args.version}-{args.revision}" if args.revision is not None else args.version
    if subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip() != "amd64":
        raise RuntimeError("Build this package on amd64")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gold-deb-") as temporary:
        work = Path(temporary)
        source = work / "source"
        shutil.copytree(
            args.source, source,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "dist", "node_modules"),
        )
        upstream_version = set_release_version(source / "setup.py", args.version)
        wheels = work / "wheels"
        run(
            "/opt/venv/bin/python", "-m", "pip", "wheel", "--no-deps", "--no-index",
            "--no-build-isolation", "--wheel-dir", wheels, source,
        )
        wheel = next(wheels.glob("gold_blockchain-*.whl"))
        stage = work / "root"
        site, patched_execstack = copy_runtime(
            source, stage, wheel, python_version, args.bundled_python,
            portable=args.target_ubuntu == "universal",
        )
        copy_units(source, stage)
        provenance = {
            "package": "gold-blockchain-cli",
            "version": debian_version,
            "cli_version": args.version,
            "upstream_source_version": upstream_version,
            "architecture": "amd64",
            "cli_path": "/usr/local/bin/gold",
            "target_os": f"Ubuntu {args.target_ubuntu}",
            "python_version": python_version,
            "python_runtime": "bundled" if args.bundled_python is not None else "system",
            "python_full_version": sys.version,
            "base_image_id": args.base_image_id,
            "source_revision": args.source_revision,
            "gold_rs_revision": args.native_revision,
            "recovery_timeout_seconds": 15,
            "cleared_executable_stack_libraries": patched_execstack,
            "supported_ubuntu": (
                ["20.04", "22.04", "24.04", "26.04"]
                if args.target_ubuntu == "universal" else [args.target_ubuntu]
            ),
            "source_sha256": {
                name: hashlib.sha256((site / name).read_bytes()).hexdigest()
                for name in (
                    "chia/timelord/timelord.py",
                    "chia/timelord/timelord_api.py",
                    "chia/full_node/full_node.py",
                    "chia/full_node/full_node_api.py",
                    "chia/full_node/full_node_store.py",
                    "chia/full_node/candidate_refresh.py",
                    "chia/consensus/multiprocess_validation.py",
                    "chia/_tests/timelord/test_peak_recovery.py",
                    "chia/_tests/timelord/test_iteration_consistency.py",
                    "chia/_tests/timelord/test_candidate_refresh.py",
                )
            },
            "vdf_client_sha256": hashlib.sha256((site / "vdf_client").read_bytes()).hexdigest(),
            "vdf_bench_sha256": hashlib.sha256((site / "vdf_bench").read_bytes()).hexdigest(),
        }
        write_file(stage / "opt/gold/BUILD-INFO.json", json.dumps(provenance, indent=2) + "\n")
        docs = stage / "usr/share/doc/gold-blockchain-cli"
        shutil.copytree(source / "build_scripts/timelord-recovery", docs / "build", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(source / "LICENSE", docs / "copyright")
        size = sum(path.stat().st_size for path in stage.rglob("*") if path.is_file() and not path.is_symlink())
        write_file(
            stage / "DEBIAN/control",
            "Package: gold-blockchain-cli\n"
            f"Version: {debian_version}\n"
            "Architecture: amd64\nSection: net\nPriority: optional\n"
            "Maintainer: Local Gold Build <root@localhost>\n"
            f"Installed-Size: {(size + 1023) // 1024}\n"
            f"Depends: {target['depends']}\n"
            "Description: Gold blockchain CLI with native timelord\n"
            " Includes full node, farmer, harvester, wallet, timelord and VDF workers.\n"
            " Local recovery build with a 15-second recovery grace period.\n",
        )
        reload_units = (
            "#!/bin/sh\nset -e\n"
            'if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then\n'
            "    systemctl daemon-reload || true\nfi\nexit 0\n"
        )
        write_file(stage / "DEBIAN/postinst", reload_units, 0o755)
        write_file(stage / "DEBIAN/postrm", reload_units, 0o755)
        # Normalize permissions inherited from Windows while preserving executable files.
        for path in stage.rglob("*"):
            if path.is_symlink():
                continue
            if path.is_dir():
                path.chmod(0o755)
            elif path.suffix in {".pem", ".crt", ".py", ".json", ".yaml", ".hex", ".clsp", ".clib", ".service"}:
                path.chmod(0o644)
        if args.filename_suffix is not None:
            suffix = args.filename_suffix
        else:
            suffix = f"_ubuntu{args.target_ubuntu}" if args.include_ubuntu_in_filename else ""
        package = args.output / f"gold-blockchain-cli_{debian_version}_amd64{suffix}.deb"
        run("dpkg-deb", "--build", "--root-owner-group", "-Zxz", stage, package)
        (args.output / "build-info.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        checksum = hashlib.sha256(package.read_bytes()).hexdigest()
        (args.output / f"{package.name}.sha256").write_text(f"{checksum}  {package.name}\n", encoding="ascii")
        print(f"Created {package}: {package.stat().st_size} bytes, SHA256 {checksum}", flush=True)


if __name__ == "__main__":
    main()
