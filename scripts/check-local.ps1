[CmdletBinding()]
param(
    [switch]$Full
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "load-local-env.ps1")
$costweaveVenvPython = Join-Path $script:CostWeaveProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $costweaveVenvPython -PathType Leaf)) {
    throw "The virtual environment is missing. Run scripts/setup-local.ps1 first."
}

& $costweaveVenvPython (Join-Path $PSScriptRoot "local-smoke.py")
if ($LASTEXITCODE -ne 0) {
    throw "Local smoke check failed."
}

if ($Full) {
    & $costweaveVenvPython -B -m unittest discover -s tests -p "test_*.py" -q
} else {
    & $costweaveVenvPython -B -m unittest `
        tests.test_model_catalog `
        tests.test_catalog_store `
        tests.test_gateway_backend_v066 `
        tests.test_small_budget_shadow_v066_wp07 `
        tests.test_provider_setup `
        tests.test_real_smoke_eval `
        -q
}
if ($LASTEXITCODE -ne 0) {
    throw "CostWeave tests failed."
}

Write-Host "CostWeave local environment check passed."
