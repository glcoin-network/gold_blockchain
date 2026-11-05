param(
    [string]$PackageImage = "gold-universal-deb:2.0.5",
    [int]$RuntimeSeconds = 300
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$output = Join-Path (Split-Path $repo -Parent) "artifacts/releases/v2.0.5/universal"
New-Item -ItemType Directory -Force -Path $output | Out-Null
$packageContainer = "gold-universal-export-20261105"

function Invoke-DockerChecked {
    param([string[]]$Arguments, [string]$Log)
    if ($Log) {
        & docker @Arguments 2>&1 | Tee-Object -FilePath $Log
    } else {
        & docker @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments[0]) failed with exit code $LASTEXITCODE"
    }
}

Invoke-DockerChecked -Arguments @("create", "--name", $packageContainer, $PackageImage)
Invoke-DockerChecked -Arguments @("cp", "${packageContainer}:/universal-out/.", $output)
New-Item -ItemType Directory -Force -Path (Join-Path $output "build-scripts") | Out-Null
Invoke-DockerChecked -Arguments @("cp", "${packageContainer}:/build/gold-src/build_scripts/timelord-recovery/.", (Join-Path $output "build-scripts"))
Invoke-DockerChecked -Arguments @("rm", $packageContainer)
$package = Join-Path $output "gold-blockchain-cli_2.0.5_amd64.deb"
$expected = (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash.ToLowerInvariant()
$targets = @(
    @{ Version = "20.04"; Base = "ubuntu:20.04" },
    @{ Version = "22.04"; Base = "gold-ubuntu-base:22.04" },
    @{ Version = "24.04"; Base = "ubuntu:24.04" },
    @{ Version = "26.04"; Base = "ubuntu:26.04.1" }
)
$results = @()
Push-Location $repo
try {
    foreach ($target in $targets) {
        $version = $target.Version
        $directory = Join-Path $output $version
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
        $image = "gold-universal-acceptance:2.0.5-ubuntu$version"
        $container = "gold-universal-acceptance-$version-20261105"
        Write-Output "Building clean Ubuntu $version acceptance environment"
        Invoke-DockerChecked -Log (Join-Path $directory "build.log") -Arguments @(
            "build", "--platform", "linux/amd64",
            "--file", "build_scripts/timelord-recovery/Dockerfile.universal-acceptance",
            "--build-arg", "PACKAGE_IMAGE=$PackageImage",
            "--build-arg", "UBUNTU_BASE=$($target.Base)", "--tag", $image, "."
        )
        Write-Output "Starting Ubuntu $version runtime acceptance, $RuntimeSeconds seconds"
        & docker run --name $container --network none --cpus 4 --memory 3072m `
            -e "GOLD_ACCEPTANCE_SECONDS=$RuntimeSeconds" $image 2>&1 |
            Tee-Object -FilePath (Join-Path $directory "runtime.log")
        $runtimeExit = $LASTEXITCODE
        Invoke-DockerChecked -Arguments @("cp", "${container}:/results/.", $directory)
        if ($runtimeExit -ne 0) {
            throw "Ubuntu $version runtime acceptance failed; see $directory"
        }
        foreach ($report in @(
            "portable-runtime-verification.json", "deb-installation-verification.json",
            "deb-vdf-verification.json"
        )) {
            Invoke-DockerChecked -Arguments @("cp", "${container}:/out/$report", (Join-Path $directory $report))
        }
        $native = Get-Content -LiteralPath (Join-Path $directory "portable-runtime-verification.json") -Raw | ConvertFrom-Json
        $runtime = Get-Content -LiteralPath (Join-Path $directory "runtime-acceptance.json") -Raw | ConvertFrom-Json
        $installation = Get-Content -LiteralPath (Join-Path $directory "deb-installation-verification.json") -Raw | ConvertFrom-Json
        if ($native.status -ne "passed" -or $runtime.status -ne "passed") {
            throw "Ubuntu $version did not pass all checks"
        }
        if ($native.package_sha256 -ne $expected -or $installation.sha256 -ne $expected) {
            throw "Ubuntu $version checked a different deb"
        }
        $results += @{
            ubuntu = $version; status = "passed"; package_sha256 = $expected
            runtime_seconds = $runtime.observed_runtime_seconds
            native_elf_files = $native.elf_files_checked
            reinstall_preserves_data = $installation.install_over_existing_package_preserves_data
        }
        $results | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output "matrix-progress.json") -Encoding utf8
        Write-Output "Ubuntu $version passed with identical SHA256 $expected"
    }
    @{
        status = "passed"; package_sha256 = $expected; targets = $results
        mainnet_tested = $false; production_database_tested = $false
        systemd_boot_tested = $false; architecture = "amd64"
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output "matrix-acceptance.json") -Encoding utf8
} finally {
    Pop-Location
}
