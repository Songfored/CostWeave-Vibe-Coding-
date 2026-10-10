$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

Push-Location $Root
try {
    & $Python -B -m costweave.aurora_rescue_eval --offline
    if ($LASTEXITCODE -ne 0) {
        throw "Aurora offline acceptance failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
