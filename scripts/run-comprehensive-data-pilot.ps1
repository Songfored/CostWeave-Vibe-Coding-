param(
    [double]$BudgetUsd = 1.25,
    [switch]$Preflight,
    [switch]$RepeatAuthorized
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "load-local-env.ps1")
$python = Join-Path $root ".venv\Scripts\python.exe"

if ($Preflight) {
    & $python -B -m costweave.comprehensive_data_pilot --preflight --root $root
    exit $LASTEXITCODE
}

if (-not $RepeatAuthorized) {
    throw "Repeat experiment is disabled without -RepeatAuthorized."
}

$oldExperimental = $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS
$oldPaid = $env:COSTWEAVE_PAID_CALLS
$oldRepeatAuthorized = $env:COSTWEAVE_REPEAT_EXPERIMENT_AUTHORIZED
try {
    $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS = "1"
    $env:COSTWEAVE_PAID_CALLS = "1"
    $env:COSTWEAVE_REPEAT_EXPERIMENT_AUTHORIZED = "1"
    & $python -B -m costweave.comprehensive_data_pilot --budget-usd $BudgetUsd --root $root
    exit $LASTEXITCODE
}
finally {
    $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS = $oldExperimental
    $env:COSTWEAVE_PAID_CALLS = $oldPaid
    $env:COSTWEAVE_REPEAT_EXPERIMENT_AUTHORIZED = $oldRepeatAuthorized
}
