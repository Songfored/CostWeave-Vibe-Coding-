param(
    [double]$BudgetUsd = 0,
    [double]$BudgetCny = 0,
    [switch]$Preflight,
    [switch]$FreshReplay,
    [switch]$PaidAuthorized
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "load-local-env.ps1")
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if ($Preflight) {
    $Arguments = @('-B','-m','costweave.aurora_real_pilot','--preflight','--root',$Root)
    if ($BudgetUsd -gt 0) { $Arguments += @('--budget-usd',$BudgetUsd) }
    if ($BudgetCny -gt 0) { $Arguments += @('--budget-cny',$BudgetCny) }
    if ($FreshReplay) { $Arguments += '--fresh-replay' }
    & $Python @Arguments
    exit $LASTEXITCODE
}
if (-not $PaidAuthorized) {
    throw "Aurora paid execution requires -PaidAuthorized."
}
if ($BudgetCny -le 0) {
    throw "Aurora paid execution requires an explicitly authorized -BudgetCny value."
}

$OldExperimental = $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS
$OldPaid = $env:COSTWEAVE_PAID_CALLS
$OldAurora = $env:COSTWEAVE_AURORA_PAID_AUTHORIZED
try {
    $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS = "1"
    $env:COSTWEAVE_PAID_CALLS = "1"
    $env:COSTWEAVE_AURORA_PAID_AUTHORIZED = "1"
    $Arguments = @('-B','-m','costweave.aurora_real_pilot','--budget-cny',$BudgetCny,'--root',$Root)
    if ($BudgetUsd -gt 0) { $Arguments += @('--budget-usd',$BudgetUsd) }
    if ($FreshReplay) { $Arguments += '--fresh-replay' }
    & $Python @Arguments
    exit $LASTEXITCODE
}
finally {
    $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS = $OldExperimental
    $env:COSTWEAVE_PAID_CALLS = $OldPaid
    $env:COSTWEAVE_AURORA_PAID_AUTHORIZED = $OldAurora
}
