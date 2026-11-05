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

## Peak Recovery Scope

The earlier peak-recovery patch changes only `chia/timelord/timelord.py` and
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

The checkout and CLI packages declare version `2.0.5`. This CLI release does not
rebuild the GUI installers.
Gold retains its standard DNS-seeder, introducer, and address-manager discovery
flow. Introducer IP addresses are not pinned or permanently preferred.

Run the recovery regressions against the installed development checkout:

```powershell
$env:GOLD_TEST_REAL_IMPORTS = "1"
python chia/_tests/timelord/test_peak_recovery.py
```

The tests also support a dependency-free source-extracted mode when
`GOLD_TEST_REAL_IMPORTS` is unset. That mode does not validate installed imports.

## Candidate Iteration Consistency

Candidate construction now requests the stake coefficient at its selected
parent's height, matching unfinished-header validation, instead of defaulting to
the current tip. This corrects a reproducible mismatch when the SP parent is
older than the current tip. The stake formula and consensus validation are
unchanged.

Timelord checks the candidate's signed total iterations against the current
slot's origin and calculated infusion point before scheduling and again before
publishing a proof or advancing speculatively. Pending overflow work is retained
until its correct sub-slot. Completed work with no usable candidate is retired
so it cannot hold up later signage points or the end of the slot.

Rejected candidates are not rewritten or accepted by weakening validation.
Logs now retain expected versus actual total iterations, the validation
coefficient and slot parameters, and original versus final parent hashes.
These diagnostics are necessary because changing the final infusion parent can
change the stake context after an unfinished block has already been signed.
Timelord alone does not have the coin history to re-evaluate that context.

The tests prove the specific construction and scheduling cases above. They do
not replay the 24 historical rejected candidates, which were not serialized in
the supplied log, or establish that every mainnet rejection has been eliminated.
Sustained mainnet observation remains required.

Run the iteration regressions with installed dependencies:

```powershell
python chia/_tests/timelord/test_iteration_consistency.py -v
```

The image and deb verification stages run these tests as well as the existing
peak-recovery tests. The new suite uses test-only constants and a stubbed space
quality to isolate iteration accounting; BLS signatures and VDF header checks
use the real native implementations.

## Pending Candidate Refresh

The iteration consistency checks alone do not repair a candidate signed before
an intervening parent changes the height-dependent stake coefficient. The final
header still has to satisfy the existing coefficient at its final parent height.
Changing the signed total iterations without a new plot signature is invalid.

Full Node now tracks the Farmer connection that supplied each local candidate.
On a new peak or accepted end of slot, it recalculates a still-pending candidate
on the descendant parent using the existing quality, difficulty, and stake
formula. It requests a new foliage signature through the existing Farmer and
Harvester protocol before publishing that candidate. Candidates whose SP
signatures or transaction context cannot be reused are not rewritten. Reward
destinations are preserved. The transaction section follows the unchanged
previous-transaction-block rule.

Before notifying Timelord of a peak, Full Node sends pending candidate stake
contexts in the same message batch. Timelord retires work whose committed total
no longer matches that context. If the coefficient changes but iteration
rounding does not, submitted proofs are retained. Timelord using its outbound
loopback Full Node waits for that node's confirmation of a speculative parent
before emitting another infusion; VDF computation is not stopped for the wait.
Remote peers cannot override that local candidate context or release the wait.
The existing rejected-infusion recovery remains available. A lost confirmation
recovers to the saved confirmed parent after the existing 15-second grace
period; a closed local connection triggers that recovery immediately. The
saved parent's reward-challenge history is copied so speculative calculation
cannot mutate the recovery snapshot. Recovery discards stale queued candidates
and logs the reason instead of submitting them with an unconfirmed parent.

This adds no protocol fields and changes neither consensus validators, the
stake formula, nor plot-signature requirements. Full Node and Timelord must
both run the repaired source. Farmers whose Full Nodes have not been updated
cannot be re-signed by another Full Node. The local-context ordering and
confirmation behavior is scoped to the outbound loopback Full Node; a
Timelord configured exclusively against another machine needs separate
deployment work and acceptance.

Run the refresh regressions with installed native dependencies:

```powershell
$env:GOLD_TEST_REAL_IMPORTS = "1"
python -m unittest chia._tests.timelord.test_candidate_refresh
```

The suite reproduces stale-parent rejection, rejects iteration changes without
fresh signatures, and accepts a re-signed candidate through unchanged header
and body validation into a disposable SQLite chain. It also checks late
signatures, repeated parent changes, transaction-section preservation, overflow
slot selection, context-before-peak ordering, the confirmation wait, lost
confirmations, local disconnection, and recovery-history isolation. Space
quality is stubbed in these small-chain fixtures; separate native-plot checks
exercise the Farmer/Harvester re-signing protocol. Historical rejected blocks
were not serialized in the logs, so they cannot be fully replayed. These tests
do not establish mainnet error elimination or recovered rewards.

## Ubuntu 20.04 Package

After building the recovery image, run from the parent directory:

```powershell
docker build --platform linux/amd64 `
  --file g-timelord/build_scripts/timelord-recovery/Dockerfile.deb `
  --tag gold-timelord-deb-verified:2.0.5-ubuntu20.04-amd64 .
```

The package is available at `/out/gold-blockchain-cli_2.0.5_amd64.deb`.
The package builds support `GOLD_SOURCE_REVISION`, `GOLD_NATIVE_REVISION`, and
`VERIFIED_IMAGE_ID` build arguments for provenance. Omitted values are recorded
as `unknown` instead of identifying an unrelated historical build.

## Ubuntu 24.04 Package

The Ubuntu 20.04 deb uses Python 3.8, libssl1.1 and libffi7. For Ubuntu 24.04,
build the separate Python 3.12 package and verify it in a clean Noble image:

```powershell
docker build --platform linux/amd64 `
  --file g-timelord/build_scripts/timelord-recovery/Dockerfile.ubuntu24.04 `
  --tag gold-timelord-deb-verified:2.0.5-ubuntu24.04-amd64 .
docker run --name gold-deb-noble-acceptance --network none `
  --cpus 4 --memory 3072m -e GOLD_ACCEPTANCE_SECONDS=480 `
  gold-timelord-deb-verified:2.0.5-ubuntu24.04-amd64
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

The package has Debian and Gold version `2.0.5`, with filename
`gold-blockchain-cli_2.0.5_amd64.deb`. Its CLI entry point
is `/usr/local/bin/gold`; `/usr/bin/gold` belongs to Ubuntu's `binutils` GNU Gold
linker and is verified to remain intact during package installation. Do not use
the earlier Ubuntu 24.04 `2.1-1` package, whose CLI symlink collided with `binutils`.
Keep Ubuntu-specific artifacts in separate directories. Systemd lifecycle, production
databases, real plots and mainnet transactions require separate acceptance.

## Offline Ubuntu 24.04 Repackaging

When the verified Python 3.12 dependency image is already available, rebuild the
application wheel and deb offline without recompiling unchanged native libraries.
This path uses the backend directory itself as the build context:

```powershell
docker build --platform linux/amd64 --network none `
  --file g-timelord/build_scripts/timelord-recovery/Dockerfile.repackage-ubuntu24.04 `
  --tag gold-timelord-deb-verified:2.0.5-ubuntu24.04-amd64 g-timelord
```

The default dependency image is
`gold-timelord-deb-verified:2.1-2-ubuntu24.04-amd64`. Override `VERIFIED_RUNTIME`
to use another verified Ubuntu 24.04/Python 3.12 runtime. Record its image ID,
the source base revision with an uncommitted-change marker when applicable,
and the compiled native revision using the same provenance arguments documented
above. The new image installs the new deb, runs all three installed regression suites
and validates a native VDF proof. Its runtime command also checks the six-service
lifecycle and removal/reinstallation. No host node data is needed.

The package and CLI must both report `2.0.5`; the unchanged dependency image's
older tag does not identify the new release. Keep Ubuntu 20.04 and 24.04 exports
in separate directories even though their requested filenames are identical.

## Ubuntu 26.04.1 Package

The 26.04.1 package bundles the Python 3.12 interpreter, standard library and
dependencies from the verified 2.0.5 Ubuntu 24.04 runtime under `/opt/gold`.
The host's Python 3.14 is not used by Gold. This preserves the existing native
bindings without upgrading PyO3 or changing consensus dependencies. Build the
package and verify it in a clean Resolute base:

```powershell
docker build --platform linux/amd64 `
  --file g-timelord/build_scripts/timelord-recovery/Dockerfile.ubuntu26.04 `
  --tag gold-timelord-deb-verified:2.0.5-ubuntu26.04-amd64 .
docker run --name gold-deb-resolute-acceptance --network none `
  --cpus 4 --memory 3072m -e GOLD_ACCEPTANCE_SECONDS=480 `
  gold-timelord-deb-verified:2.0.5-ubuntu26.04-amd64
```

Override `VERIFIED_RUNTIME` to select another verified Ubuntu 24.04/Python 3.12
runtime. The default is `gold-candidate-refresh-deb-test:2.0.5-20261103`; record
its image ID and native/source revisions with the provenance arguments above.
The build clears malformed executable-stack declarations from native shared
libraries, currently the `chiapos` BladeBit harvester library. It does not alter
consensus, staking, block validation, or Timelord logic. The package is exported
as `gold-blockchain-cli_2.0.5_amd64_ubuntu26.04.1.deb`. System library dependencies
are installed by apt from the target release; no old Ubuntu apt sources are used.
