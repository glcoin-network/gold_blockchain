# Timelord Recovery Image

This image builds the current Gold source and `gold_rs` 0.9.0 on Ubuntu 20.04
for amd64. Its default command prints CLI help; building or starting it without
additional arguments does not start a mainnet node.

The build context must contain these sibling directories:

```text
g-timelord/
gold_rs/
```

For a fresh workspace:

```powershell
git clone https://github.com/glcoin-network/gold_blockchain.git g-timelord
git clone https://github.com/glcoin-network/gold_rs.git gold_rs
```

From their parent directory:

```powershell
docker build --platform linux/amd64 `
  --file g-timelord/build_scripts/timelord-recovery/Dockerfile `
  --tag gold-timelord-recovery:ubuntu20.04-amd64 .
docker run --rm gold-timelord-recovery:ubuntu20.04-amd64 version
```

Read the build verification report without starting services:

```powershell
docker run --rm --network none --entrypoint cat `
  gold-timelord-recovery:ubuntu20.04-amd64 /opt/gold-image-verification.json
```

Check initialization in a disposable container with no host data mounted:

```powershell
docker run --rm --network none gold-timelord-recovery:ubuntu20.04-amd64 init
```

The image includes all service commands. Its `gold start` command uses the
standard daemon behavior and returns after starting services, so a persistent
runtime needs a foreground service or a supervisor to keep its container alive.
An existing running container is not modified by building this image.

Build verification uses the installed native dependencies, exercises recovery
regressions, imports the stake/consensus, full node, farmer, harvester, timelord,
and wallet modules, and obtains and validates an actual native VDF-client proof.
Results are written to `/opt/gold-image-verification.json` inside the image.

## Scope

The recovery patch changes only `chia/timelord/timelord.py` and
`chia/timelord/timelord_api.py`. It preserves message serialization, consensus
validation, the stake coefficient formula, and the full node, farmer, harvester,
and wallet implementations.

When the unfinished-block preference would otherwise discard a correction,
timelord remembers a recent ancestor sent by its own outbound loopback full-node
connection. After a 15-second grace period, if no accepted peak has superseded it,
timelord discards its pending unfinished/overflow work and uses the existing chain
reset to return to that ancestor. Normal confirmations cancel recovery.

Recovery is limited to a known ancestor exactly one height behind at receipt,
with lower weight and matching recorded total iterations. A disconnected,
remote, or inbound connection cannot schedule this recovery. Deployments with a
full node on another machine retain their previous behavior.

This fixes a delayed recovery path after a consensus rejection. It does not
change the initial rejection rule, guarantee a fixed time for every block, or
prove that the original coefficient mismatch has been eliminated. Mainnet
acceptance must check sustained production and all six component workflows
before replacing the deployed package.

The checkout and packages declare version `2.0.3`, matching the GUI release.
Gold retains its standard DNS-seeder, introducer, and address-manager discovery
flow. Introducer IP addresses are not pinned or permanently preferred.

Run the recovery regressions against the installed development checkout:

```powershell
$env:GOLD_TEST_REAL_IMPORTS = "1"
python chia/_tests/timelord/test_peak_recovery.py
```

The tests also support a dependency-free source-extracted mode when
`GOLD_TEST_REAL_IMPORTS` is unset. That mode does not validate installed imports.

## Ubuntu 20.04 Package

After building the recovery image, run from the parent directory:

```powershell
docker build --platform linux/amd64 `
  --file g-timelord/build_scripts/timelord-recovery/Dockerfile.deb `
  --tag gold-timelord-deb-verified:2.0.3-ubuntu20.04-amd64 .
```

The package is available at `/out/gold-blockchain-cli_2.0.3_amd64.deb`.
The package builds support `GOLD_SOURCE_REVISION`, `GOLD_NATIVE_REVISION`, and
`VERIFIED_IMAGE_ID` build arguments for provenance. Omitted values are recorded
as `unknown` instead of identifying an unrelated historical build.

## Ubuntu 24.04 Package

The Ubuntu 20.04 deb uses Python 3.8, libssl1.1 and libffi7. For Ubuntu 24.04,
build the separate Python 3.12 package and verify it in a clean Noble image:

```powershell
docker build --platform linux/amd64 `
  --file g-timelord/build_scripts/timelord-recovery/Dockerfile.ubuntu24.04 `
  --tag gold-timelord-deb-verified:2.0.3-ubuntu24.04-amd64 .
docker run --name gold-deb-noble-acceptance --network none `
  --cpus 4 --memory 3072m -e GOLD_ACCEPTANCE_SECONDS=480 `
  gold-timelord-deb-verified:2.0.3-ubuntu24.04-amd64
```

This build reuses only the Rust toolchain and Cargo cache from the local recovery
image. It compiles gold_rs and VDF for Python 3.12 on Ubuntu 24.04, builds the deb
in `/out`, and installs it with apt in a fresh Ubuntu 24.04 stage. The runtime
command checks services, native proofs, wallet data preservation, SQLite, and
removal/reinstallation. Export `/results` and `/out/deb-installation-verification.json`
before removing the acceptance container.

The 2-CPU, 1536-MiB Noble run timed out on RPC. An unchanged package completed
the 4-CPU, 3072-MiB functional checks; this is a test allocation, not a production
minimum. Review all ERROR records, including launcher connection refusals during
planned shutdowns, alongside the recorded stop windows.

The package has Debian and Gold version `2.0.3`, with filename
`gold-blockchain-cli_2.0.3_amd64_ubuntu24.04.deb`. Its CLI entry point
is `/usr/local/bin/gold`; `/usr/bin/gold` belongs to Ubuntu's `binutils` GNU Gold
linker and is verified to remain intact during package installation. Do not use
the earlier Ubuntu 24.04 `2.1-1` package, whose CLI symlink collided with `binutils`.
Keep Ubuntu-specific artifacts in separate directories. Systemd lifecycle, production
databases, real plots and mainnet transactions require separate acceptance.
