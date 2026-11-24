# SPDX-License-Identifier: AGPL-3.0-only
# One-step installer for the Unsloth Studio Qualcomm Hexagon NPU Bridge

$ErrorActionPreference = "Stop"

Write-Host "=== Unsloth Studio - Qualcomm Hexagon NPU Bridge Installer (Update-Proof .pth Shim) ===" -ForegroundColor Cyan

$studioPy = Join-Path $env:USERPROFILE ".unsloth\studio\unsloth_studio\Scripts\python.exe"
if ($env:UNSLOTH_STUDIO_HOME) {
    $studioPy = Join-Path $env:UNSLOTH_STUDIO_HOME "unsloth_studio\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $studioPy)) {
    throw "Unsloth Studio Python environment not found at $studioPy. Install Unsloth Studio first."
}

$geniexCandidates = @(
    (Get-Command geniex -ErrorAction SilentlyContinue).Source,
    (Join-Path $env:LOCALAPPDATA "GenieX CLI\geniex.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\GenieX CLI\geniex.exe"),
    (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\geniex.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

if (-not $geniexCandidates) {
    Write-Host "[1/2] Installing Qualcomm GenieX CLI via winget..." -ForegroundColor Yellow
    winget install --id Qualcomm.GenieX.CLI --exact --accept-package-agreements --accept-source-agreements
} else {
    Write-Host "[1/2] Qualcomm GenieX CLI found at $($geniexCandidates[0])" -ForegroundColor Green
}

Write-Host "[2/2] Installing zero-modification, update-proof .pth runtime shim..." -ForegroundColor Yellow
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& $studioPy (Join-Path $scriptDir "apply_patch.py") --apply
& $studioPy (Join-Path $scriptDir "apply_patch.py") --status

Write-Host "`nDone! Studio files remain 100% stock and survive future Unsloth Studio updates." -ForegroundColor Green
Write-Host "Restart Unsloth Studio, then find NPU models in:" -ForegroundColor Cyan
Write-Host "  1. Model Hub -> Format dropdown -> NPU" -ForegroundColor White
Write-Host "  2. Chat -> Model Picker -> NPU format filter" -ForegroundColor White
