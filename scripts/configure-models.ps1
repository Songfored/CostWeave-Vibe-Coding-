[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$costweaveProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$costweaveVenvPython = Join-Path $costweaveProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $costweaveVenvPython -PathType Leaf)) {
    throw "本地环境尚未安装，请先运行 .\scripts\setup-local.ps1"
}

Set-Location -LiteralPath $costweaveProjectRoot
Write-Host "CostWeave 将自动使用已经确认的平台地址和协议。"
Write-Host "已存在的密钥会自动复用；缺少密钥时才会提示输入。"
Write-Host "输入密钥时屏幕不会显示字符，这是正常现象。"
Write-Host "本步骤只查询模型列表，不会发送付费推理请求。"
Write-Host ""

& $costweaveVenvPython -B -m costweave.provider_setup configure
if ($LASTEXITCODE -ne 0) {
    throw "模型接口配置未完成。"
}
