"""Relocate the Focal runtime without exporting a process-wide library path."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess


HOST_LIBRARIES = {
    "libc.so.6", "libpthread.so.0", "libdl.so.2", "libutil.so.1",
    "libm.so.6", "librt.so.1", "libresolv.so.2", "libanl.so.1",
    "libBrokenLocale.so.1", "ld-linux-x86-64.so.2",
    "libstdc++.so.6", "libgcc_s.so.1",
}
ABI_LIMITS = {"GLIBC": (2, 31), "GLIBCXX": (3, 4, 28), "CXXABI": (1, 3, 12)}


def run(*arguments):
    return subprocess.check_output([str(value) for value in arguments], text=True).strip()


def elf_paths(root):
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            header = stream.read(18)
            if header[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", header, 16)[0] in (2, 3):
                yield path


def elf_headers(data):
    if data[:6] != b"\x7fELF\x02\x01":
        raise RuntimeError("Only little-endian ELF64 is supported")
    return struct.unpack_from("<16sHHIQQQIHHHHHH", data)


def code_hashes(path):
    data = path.read_bytes()
    header = elf_headers(data)
    section_offset, section_size, section_count = header[6], header[11], header[12]
    strings = struct.unpack_from("<IIQQQQIIQQ", data, section_offset + header[13] * section_size)
    names = data[strings[4]:strings[4] + strings[5]]
    hashes = {}
    for index in range(section_count):
        section = struct.unpack_from("<IIQQQQIIQQ", data, section_offset + index * section_size)
        if section[2] & 4:  # SHF_EXECINSTR
            name = names[section[0]:].split(b"\0", 1)[0].decode("ascii")
            hashes[name] = hashlib.sha256(data[section[4]:section[4] + section[5]]).hexdigest()
    if not hashes:
        raise RuntimeError(f"No executable sections in {path}")
    return hashes


def clear_bladebit_stack(path):
    if ".so" not in path.name:
        return False
    data = bytearray(path.read_bytes())
    header = elf_headers(data)
    changed = False
    for index in range(header[10]):
        offset = header[5] + index * header[9]
        kind, flags = struct.unpack_from("<II", data, offset)
        if kind == 0x6474E551 and flags & 1:  # PT_GNU_STACK, PF_X
            if path.parent.name != "chiapos.libs" or not path.name.startswith("libbladebit_harvester-"):
                raise RuntimeError(f"Unexpected executable-stack requirement: {path}")
            struct.pack_into("<I", data, offset + 4, flags & ~1)
            changed = True
    if changed:
        path.write_bytes(data)
    return changed


def abi_requirements(path):
    versions = {}
    text = run("readelf", "--version-info", "-W", path)
    for name, value in re.findall(r"\b(GLIBCXX|GLIBC|CXXABI)_(\d+(?:\.\d+)+)\b", text):
        version = tuple(map(int, value.split(".")))
        if version > ABI_LIMITS[name]:
            raise RuntimeError(f"{path} requires {name}_{value}, exceeding the Focal baseline")
        if version > versions.get(name, ()):
            versions[name] = version
    return {name: ".".join(map(str, version)) for name, version in versions.items()}


def library_owner(path):
    for candidate in (path, path.resolve()):
        query = subprocess.run(["dpkg-query", "-S", str(candidate)], text=True, capture_output=True)
        if query.returncode == 0:
            return query.stdout.split(": ", 1)[0].strip()
    raise RuntimeError(f"No package owns dependency {path}")


def prepare_portable_runtime(stage):
    application = stage / "opt/gold"
    private_lib = application / "lib"
    private_lib.mkdir()
    originals = list(elf_paths(application))
    dependencies = {}
    owners = {"python3.8", "python3.8-minimal", "libpython3.8-stdlib", "libpython3.8-minimal"}
    for path in originals:
        links = run("ldd", path)
        if "not found" in links:
            raise RuntimeError(f"Unresolved original dependencies in {path}: {links}")
        for name, target in re.findall(r"^\s*(\S+)\s+=>\s+(/\S+)", links, re.MULTILINE):
            source = Path(target)
            if name in HOST_LIBRARIES or name.startswith("libnss_"):
                continue
            if not str(source).startswith(("/lib/", "/usr/lib/")):
                continue
            previous = dependencies.get(name)
            if previous is not None and previous != source.resolve():
                raise RuntimeError(f"Conflicting dependency {name}: {previous}, {source}")
            dependencies[name] = source.resolve()
            owners.add(library_owner(source))
    for name, source in sorted(dependencies.items()):
        shutil.copyfile(source, private_lib / name)
        (private_lib / name).chmod(0o644)
    licenses = stage / "usr/share/doc/gold-blockchain-cli/runtime-licenses"
    packages = {}
    for owner in sorted(owners):
        package = owner.split(":")[0]
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if not copyright_file.is_file():
            raise RuntimeError(f"Missing dependency copyright: {owner}")
        destination = licenses / package
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(copyright_file, destination / "copyright")
        packages[owner] = run("dpkg-query", "-W", "-f=${Version}", owner)
    audit = {}
    cleared = []
    for path in elf_paths(application):
        before = code_hashes(path)
        if clear_bladebit_stack(path):
            cleared.append(str(path.relative_to(application / "venv/lib/python3.8/site-packages")))
        existing = run("patchelf", "--print-rpath", path)
        relative = os.path.relpath(private_lib, path.parent)
        private_rpath = "$ORIGIN" if relative == "." else f"$ORIGIN/{relative}"
        search = list(dict.fromkeys([part for part in existing.split(":") if part] + [private_rpath]))
        subprocess.run(["patchelf", "--set-rpath", ":".join(search), str(path)], check=True)
        if code_hashes(path) != before:
            raise RuntimeError(f"Executable code changed while relocating {path}")
        audit[str(path.relative_to(application))] = {
            "rpath": run("patchelf", "--print-rpath", path),
            "required_abi": abi_requirements(path),
            "executable_sections_sha256": before,
        }
    report = {
        "supported_ubuntu": ["20.04", "22.04", "24.04", "26.04"],
        "baseline": "Ubuntu 20.04 amd64",
        "glibc_bundled": False,
        "process_wide_library_path_required": False,
        "cleared_executable_stack_libraries": cleared,
        "private_libraries": {
            name: {"source": str(source), "sha256": hashlib.sha256((private_lib / name).read_bytes()).hexdigest()}
            for name, source in sorted(dependencies.items())
        },
        "bundled_dependency_packages": packages,
        "elf_audit": audit,
    }
    (application / "PORTABLE-RUNTIME.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return cleared
