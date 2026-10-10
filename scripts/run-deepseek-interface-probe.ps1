[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(0.001, 0.02)]
    [double]$BudgetUsd,
    [Parameter(Mandatory = $true)]
    [switch]$AuthorizePaidCalls
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not $AuthorizePaidCalls) {
    throw "Explicit -AuthorizePaidCalls is required."
}

. (Join-Path $PSScriptRoot "load-local-env.ps1")
$costweaveVenvPython = Join-Path $script:CostWeaveProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $costweaveVenvPython -PathType Leaf)) {
    throw "Local environment is missing. Run .\scripts\setup-local.ps1 first."
}

$oldExperimental = $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS
$oldPaid = $env:COSTWEAVE_PAID_CALLS
try {
    $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS = "1"
    $env:COSTWEAVE_PAID_CALLS = "1"
    & $costweaveVenvPython -B -m costweave.deepseek_interface_probe --budget-usd $BudgetUsd
    if ($LASTEXITCODE -ne 0) {
        throw "DeepSeek interface probe failed; no automatic retry was made."
    }
}
finally {
    $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS = $oldExperimental
    $env:COSTWEAVE_PAID_CALLS = $oldPaid
}
