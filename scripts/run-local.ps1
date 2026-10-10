[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [ValidateSet("127.0.0.1", "localhost", "::1")]
    [string]$BindAddress = "127.0.0.1"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "load-local-env.ps1")
$costweaveVenvPython = Join-Path $script:CostWeaveProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $costweaveVenvPython -PathType Leaf)) {
    throw "The virtual environment is missing. Run scripts/setup-local.ps1 first."
}

$costweaveAdaptersEnabled = $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS -eq "1"
$costweavePaidCallsEnabled = $env:COSTWEAVE_PAID_CALLS -eq "1"
Write-Host "External adapters enabled: $costweaveAdaptersEnabled"
Write-Host "Paid calls enabled: $costweavePaidCallsEnabled"
if ($costweaveAdaptersEnabled -or $costweavePaidCallsEnabled) {
    Write-Warning "This is not the safe offline profile. Verify the experiment budget and consent before continuing."
}
Write-Host "Press Ctrl+C to stop the local service."
& $costweaveVenvPython -m costweave.server --host $BindAddress --port $Port
