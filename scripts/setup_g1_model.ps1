param(
    [string]$Destination = (Join-Path $PSScriptRoot "..\models\unitree_mujoco")
)

$ErrorActionPreference = "Stop"
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
$scenePath = Join-Path $destinationPath "unitree_robots\g1\scene_29dof.xml"

if (Test-Path -LiteralPath $scenePath) {
    Write-Host "Official Unitree G1 model is already ready: $scenePath"
    exit 0
}
if (Test-Path -LiteralPath $destinationPath) {
    throw "Destination exists but the G1 model is incomplete: $destinationPath"
}

git clone --depth 1 --filter=blob:none --sparse `
    https://github.com/unitreerobotics/unitree_mujoco.git $destinationPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to clone the official unitree_mujoco repository"
}

git -C $destinationPath sparse-checkout set unitree_robots/g1
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $scenePath)) {
    throw "Failed to obtain the official Unitree G1 model subtree"
}

$revision = git -C $destinationPath rev-parse HEAD
Write-Host "Official Unitree G1 model ready: $scenePath"
Write-Host "unitree_mujoco revision: $revision"
Write-Host "License: $destinationPath\LICENSE (BSD-3-Clause)"
