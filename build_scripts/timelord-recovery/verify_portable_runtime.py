"""Audit native loading on each target, without using host Python or old apt sources."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from portable_runtime import code_hashes, elf_paths


def main():
    root = Path("/opt/gold")
    info = json.loads((root / "BUILD-INFO.json").read_text())
    audit = json.loads((root / "PORTABLE-RUNTIME.json").read_text())
    release = platform.freedesktop_os_release() if hasattr(platform, "freedesktop_os_release") else {
        key: value.strip('"') for key, value in (
            line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line
        )
    }
    assert release["VERSION_ID"] in info["supported_ubuntu"], release
    assert sys.base_prefix == "/opt/gold/python", sys.base_prefix
    assert not os.environ.get("LD_LIBRARY_PATH"), "A global library path would mask relocation failures"
    links = {}
    for path in elf_paths(root):
        output = subprocess.check_output(["ldd", str(path)], text=True)
        assert "not found" not in output, (path, output)
        relative = str(path.relative_to(root))
        assert code_hashes(path) == audit["elf_audit"][relative]["executable_sections_sha256"], relative
        headers = subprocess.check_output(["readelf", "-lW", str(path)], text=True)
        if ".so" in path.name:
            assert not any(
                " RWE " in f" {line} " for line in headers.splitlines() if "GNU_STACK" in line
            ), relative
        links[relative] = output.splitlines()
    for name in ("ssl", "_ssl", "_hashlib", "ctypes", "sqlite3", "bz2", "lzma", "readline", "dbm",
                 "chiapos", "chiavdf", "gold_rs", "chia_rs"):
        importlib.import_module(name)
    assert not list((root / "lib").glob("libc.so*"))
    result = {
        "status": "passed", "os_release": release,
        "package_sha256": hashlib.sha256(Path("/out/gold-blockchain-cli_2.0.5_amd64.deb").read_bytes()).hexdigest(),
        "python": sys.version, "python_base_prefix": sys.base_prefix,
        "elf_files_checked": len(links), "dependencies": links,
        "glibc_bundled": False, "global_library_path_required": False,
        "executable_code_unchanged": True,
    }
    Path("/out/portable-runtime-verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "dependencies"}, indent=2))


if __name__ == "__main__":
    main()
