[CmdletBinding()]
param(
    [string]$GvhmrRoot = "C:\dev\GVHMR",
    [string]$GmrRoot = "C:\dev\GMR",
    [string]$WslDistribution = "",
    [switch]$SkipDownloads,
    [switch]$SkipGpuCheck
)

$ErrorActionPreference = "Stop"
$GvhmrRevision = "6ec3ca39336c50492c0fae65fba2fb831fc7d866"
$GmrRevision = "bb1bbe40774794fceb2a7c579a3464a28e68c844"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptRoot "..\..")).Path
$LocalConfig = Join-Path $ScriptRoot "environment.local.json"

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Action)
    Write-Host "[RUN] $Description"
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Assert-Checkout {
    param(
        [string]$Name,
        [string]$Path,
        [string]$Remote,
        [string]$Revision
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        Invoke-Checked "Clone $Name" { git clone $Remote $Path }
        Invoke-Checked "Pin $Name to $Revision" { git -C $Path checkout --detach $Revision }
    }
    $actual = (git -C $Path rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "$Name is not a Git checkout: $Path"
    }
    if (-not $actual.StartsWith($Revision)) {
        throw "$Name revision mismatch at $Path. Expected $Revision, found $actual. Use the pinned revision or pass the matching checkout."
    }
    git -C $Path diff --quiet --exit-code
    if ($LASTEXITCODE -ne 0) {
        throw "$Name has tracked local changes at $Path. Preserve or commit them before running reproducible setup."
    }
    Write-Host "[OK] $Name $actual"
    return $actual
}

function Convert-ToWslPath {
    param([string]$WindowsPath)
    $translated = (& wsl.exe -d $WslDistribution --exec wslpath -a $WindowsPath)
    if ($LASTEXITCODE -ne 0 -or -not $translated) {
        throw "Could not translate Windows path for WSL: $WindowsPath"
    }
    return $translated.Trim()
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "WSL2 is not installed. Install WSL2 and Ubuntu, then rerun this script."
}

$build = [System.Environment]::OSVersion.Version.Build
if ($build -lt 22000) {
    throw "Windows 11 is required for the supported local setup (detected build $build)."
}
Write-Host "[OK] Windows build $build"

$distros = (& wsl.exe -l -q) -replace "`0", "" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if (-not $WslDistribution) {
    if ("Ubuntu-22.04" -in $distros) {
        $WslDistribution = "Ubuntu-22.04"
    } elseif ("Ubuntu" -in $distros) {
        $WslDistribution = "Ubuntu"
    } elseif ($distros.Count -eq 1) {
        $WslDistribution = $distros[0]
    } else {
        throw "Could not choose a WSL distribution. Pass -WslDistribution explicitly. Installed: $($distros -join ', ')"
    }
}
if ($WslDistribution -notin $distros) {
    throw "WSL distribution '$WslDistribution' is not installed. Installed: $($distros -join ', ')"
}
Write-Host "[OK] WSL2 distribution $WslDistribution"

$gvhmrFull = [System.IO.Path]::GetFullPath($GvhmrRoot)
$gmrFull = [System.IO.Path]::GetFullPath($GmrRoot)
$gvhmrActual = Assert-Checkout "GVHMR" $gvhmrFull "https://github.com/zju3dv/GVHMR.git" $GvhmrRevision
$gmrActual = Assert-Checkout "GMR" $gmrFull "https://github.com/YanjieZe/GMR.git" $GmrRevision

$wslHome = (& wsl.exe -d $WslDistribution --exec sh -c 'printf %s "$HOME"').Trim()
if ($LASTEXITCODE -ne 0 -or -not $wslHome.StartsWith("/")) {
    throw "Could not determine the WSL home directory."
}
$environmentRoot = "$wslHome/.local/share/g1-reaction-generator"
$gvhmrPython = "$environmentRoot/envs/gvhmr/bin/reaction-python"
$gmrPython = "$environmentRoot/envs/gmr/bin/reaction-python"

$configuration = [ordered]@{
    schema_version = 1
    backend = "wsl"
    wsl_distribution = $WslDistribution
    gvhmr_root = $gvhmrFull
    gmr_root = $gmrFull
    gvhmr_python = $gvhmrPython
    gmr_python = $gmrPython
    gvhmr_revision = $gvhmrActual
    gmr_revision = $gmrActual
}
$configuration | ConvertTo-Json | Set-Content -LiteralPath $LocalConfig -Encoding utf8
Write-Host "[OK] Saved $LocalConfig"

$osName = (& wsl.exe -d $WslDistribution --exec sh -c '. /etc/os-release; printf %s "$PRETTY_NAME"').Trim()
if ($osName -notmatch "Ubuntu (20\.04|22\.04)") {
    Write-Warning "$osName is outside GMR's officially tested Ubuntu 20.04/22.04 range."
} else {
    Write-Host "[OK] $osName"
}

$gpu = (& wsl.exe -d $WslDistribution --exec sh -c "nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null || true")
if (-not $gpu) {
    if (-not $SkipGpuCheck) {
        [Console]::Error.WriteLine(@"
No NVIDIA CUDA GPU is visible inside WSL2. Official GVHMR uses explicit .cuda()
calls and cannot run on a non-NVIDIA GPU. The configuration was saved, but this
run will stop before installing or downloading large ML payloads.

Use a Windows 11 PC with a supported NVIDIA GPU and current NVIDIA WSL driver,
then rerun this same setup.ps1. -SkipGpuCheck installs dependencies for inspection
only; it does not make GVHMR CPU-compatible.
"@)
        exit 2
    }
    Write-Warning "CUDA GPU check bypassed. GVHMR generation will still fail without NVIDIA CUDA."
} else {
    Write-Host "[OK] NVIDIA GPU in WSL: $($gpu -join '; ')"
}

Write-Host "[INFO] Native Visual Studio Build Tools are not required; PyTorch3D uses the upstream Linux wheel in WSL2."

$repositoryWsl = Convert-ToWslPath $RepositoryRoot
$gvhmrWsl = Convert-ToWslPath $gvhmrFull
$gmrWsl = Convert-ToWslPath $gmrFull
$setupWsl = "$repositoryWsl/tools/reaction_generator/setup_wsl.sh"
$arguments = @($setupWsl, $environmentRoot, $gvhmrWsl, $gmrWsl)
if ($SkipDownloads) {
    $arguments += "--skip-downloads"
}
Invoke-Checked "Create isolated GVHMR and GMR environments" {
    & wsl.exe -d $WslDistribution --exec bash @arguments
}

$hostPython = Join-Path $RepositoryRoot ".venv-g1\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $hostPython)) {
    $hostPython = "python"
}
& $hostPython (Join-Path $ScriptRoot "doctor.py")
exit $LASTEXITCODE
