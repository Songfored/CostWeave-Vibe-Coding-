[CmdletBinding()]
param(
    [string]$PythonCommand = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$costweaveProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $costweaveProjectRoot

if (-not (Get-Command $PythonCommand -ErrorAction SilentlyContinue)) {
    throw "Python was not found. Install Python 3.11 or newer and run this script again."
}

$costweavePythonVersion = & $PythonCommand -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0 -or [Version]$costweavePythonVersion -lt [Version]"3.11") {
    throw "CostWeave requires Python 3.11 or newer. Found: $costweavePythonVersion"
}

$costweaveEnvFile = Join-Path $costweaveProjectRoot ".env.local"
if (-not (Test-Path -LiteralPath $costweaveEnvFile -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $costweaveProjectRoot ".env.example") -Destination $costweaveEnvFile
}

$costweaveVenvPython = Join-Path $costweaveProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $costweaveVenvPython -PathType Leaf)) {
    & $PythonCommand -m venv (Join-Path $costweaveProjectRoot ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Python virtual environment creation failed."
    }
}

& $costweaveVenvPython -c "import setuptools" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing the Python build tool required by this source checkout..."
    & $costweaveVenvPython -m pip install "setuptools>=68"
    if ($LASTEXITCODE -ne 0) {
        throw "Installing setuptools failed. Check the network connection and retry."
    }
}

& $costweaveVenvPython -m pip install --no-build-isolation --no-deps -e .
if ($LASTEXITCODE -ne 0) {
    throw "Installing CostWeave into the virtual environment failed."
}

. (Join-Path $PSScriptRoot "load-local-env.ps1") -EnvironmentFile $costweaveEnvFile
& $costweaveVenvPython (Join-Path $PSScriptRoot "local-smoke.py")
if ($LASTEXITCODE -ne 0) {
    throw "CostWeave local smoke check failed."
}

Write-Host ""
Write-Host "CostWeave local environment is ready."
Write-Host "Python: $costweavePythonVersion"
Write-Host "Configure model APIs with: .\scripts\configure-models.ps1"
Write-Host "Start it with: .\scripts\run-local.ps1"
Write-Host "Recheck it with: .\scripts\check-local.ps1"
