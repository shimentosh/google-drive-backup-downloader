<#
.SYNOPSIS
    Builds a standalone Windows executable for Google Drive Backup Downloader.

.DESCRIPTION
    Wraps PyInstaller with the project's spec file. Run from the repository root
    or from this folder; the script resolves paths either way.

    The produced executable does NOT embed credentials.json. Ship it alongside
    the .exe, or let each user place their own in %LOCALAPPDATA%\GDriveBackup.

.EXAMPLE
    .\packaging\build_exe.ps1
    .\packaging\build_exe.ps1 -OneFile
#>
[CmdletBinding()]
param(
    # Produce a single .exe instead of a folder. Slower to start, easier to copy.
    [switch]$OneFile,
    # Skip the test run before building.
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Write-Host "Project root: $root" -ForegroundColor Cyan

# Prefer the project virtual environment when present.
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
Write-Host "Python: $python" -ForegroundColor Cyan

# PyInstaller may already be present; only install it when it is missing, and
# tolerate environments created without pip (for example `uv venv`).
& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nInstalling PyInstaller..." -ForegroundColor Cyan
    & $python -m pip install --quiet --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw ("Could not install PyInstaller. Install it manually with " +
               "'$python -m pip install pyinstaller' or 'uv pip install " +
               "--python $python pyinstaller', then re-run this script.")
    }
} else {
    Write-Host "PyInstaller already installed." -ForegroundColor Cyan
}

if (-not $SkipTests) {
    Write-Host "`nRunning tests..." -ForegroundColor Cyan
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed - not building." }
}

Write-Host "`nCleaning previous build output..." -ForegroundColor Cyan
foreach ($dir in @("build", "dist")) {
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
}

$spec = Join-Path $PSScriptRoot "drive_backup.spec"
$env:GDBD_ONEFILE = if ($OneFile) { "1" } else { "0" }

Write-Host "`nBuilding (one-file: $($OneFile.IsPresent))..." -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm --clean $spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$exe = Get-ChildItem -Path "dist" -Filter "*.exe" -Recurse | Select-Object -First 1
if ($null -eq $exe) { throw "No executable was produced." }

Write-Host "`nBuild complete." -ForegroundColor Green
Write-Host "  Executable: $($exe.FullName)"
Write-Host "  Size:       $([math]::Round($exe.Length / 1MB, 1)) MB"
Write-Host ""
Write-Host "Remember: place credentials.json next to the executable, or in" -ForegroundColor Yellow
Write-Host "%LOCALAPPDATA%\GDriveBackup\credentials.json, before first run." -ForegroundColor Yellow
