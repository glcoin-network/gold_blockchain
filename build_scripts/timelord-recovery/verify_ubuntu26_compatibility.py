"""Record the loader regression and verify the private runtime on Ubuntu 26.04."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import sysconfig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-original-failure", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    site = Path(sysconfig.get_paths()["purelib"])
    probe = subprocess.run(
        [sys.executable, "-c", "import chiapos, chiavdf, gold_rs, chia_rs; print('Native imports OK')"],
        text=True, capture_output=True,
    )
    libraries = sorted(site.glob("chiapos.libs/libbladebit_harvester-*.so"))
    assert libraries, "No BladeBit harvester library found"
    stacks = {}
    for library in libraries:
        headers = subprocess.check_output(["readelf", "-lW", library], text=True)
        stacks[str(library.relative_to(site))] = [
            line.strip() for line in headers.splitlines() if "GNU_STACK" in line
        ]
    if args.expect_original_failure:
        assert probe.returncode != 0, "Original library unexpectedly loaded; regression not reproduced"
        assert "cannot enable executable stack" in probe.stderr, probe.stderr
        assert any(" RWE " in f" {line} " for lines in stacks.values() for line in lines), stacks
    else:
        assert probe.returncode == 0, probe.stderr
        assert all(lines for lines in stacks.values()), stacks
        assert not any(" RWE " in f" {line} " for lines in stacks.values() for line in lines), stacks
        assert sys.base_prefix == "/opt/gold/python", sys.base_prefix
        # Check all packaged native dependencies, including the private stdlib.
        missing = {}
        for root in (site, Path(sys.base_prefix)):
            for library in sorted(root.rglob("*.so*")):
                if not library.is_file() or library.is_symlink():
                    continue
                links = subprocess.run(["ldd", library], text=True, capture_output=True)
                if "not found" in links.stdout:
                    missing[str(library)] = links.stdout
        assert not missing, missing
    result = {
        "status": "passed",
        "original_failure_reproduced": args.expect_original_failure,
        "os_release": platform.freedesktop_os_release(),
        "libc": platform.libc_ver(),
        "python": sys.version,
        "python_base_prefix": sys.base_prefix,
        "native_import_returncode": probe.returncode,
        "native_import_stdout": probe.stdout,
        "native_import_stderr": probe.stderr,
        "bladebit_gnu_stack": stacks,
        "bladebit_sha256": {
            str(library.relative_to(site)): hashlib.sha256(library.read_bytes()).hexdigest()
            for library in libraries
        },
    }
    assert result["os_release"]["VERSION_ID"] == "26.04", result["os_release"]
    assert "26.04.1" in result["os_release"]["PRETTY_NAME"], result["os_release"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
