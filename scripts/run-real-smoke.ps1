[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(0.01, 0.50)]
    [double]$BudgetUsd,
    [Parameter(Mandatory = $true)]
    [switch]$AuthorizePaidCalls
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not $AuthorizePaidCalls) {
    throw "必须显式提供 -AuthorizePaidCalls 才能运行真实模型测试。"
}

. (Join-Path $PSScriptRoot "load-local-env.ps1")
$costweaveVenvPython = Join-Path $script:CostWeaveProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $costweaveVenvPython -PathType Leaf)) {
    throw "本地环境尚未安装，请先运行 .\scripts\setup-local.ps1"
}

$oldExperimental = $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS
$oldPaid = $env:COSTWEAVE_PAID_CALLS
try {
    $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS = "1"
    $env:COSTWEAVE_PAID_CALLS = "1"
    & $costweaveVenvPython -B -m costweave.real_smoke_eval --budget-usd $BudgetUsd
    if ($LASTEXITCODE -ne 0) {
        throw "真实模型冒烟评测未全部完成；程序已停止后续调用。"
    }
}
finally {
    $env:COSTWEAVE_EXPERIMENTAL_ADAPTERS = $oldExperimental
    $env:COSTWEAVE_PAID_CALLS = $oldPaid
}
