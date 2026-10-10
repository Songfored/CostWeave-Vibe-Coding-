[CmdletBinding()]
param(
    [string]$EnvironmentFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:CostWeaveProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($EnvironmentFile)) {
    $EnvironmentFile = Join-Path $script:CostWeaveProjectRoot ".env.local"
}
if (-not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
    throw "Missing local environment file: $EnvironmentFile. Run scripts/setup-local.ps1 first."
}

foreach ($costweaveLine in Get-Content -LiteralPath $EnvironmentFile) {
    $costweaveTrimmed = $costweaveLine.Trim()
    if (-not $costweaveTrimmed -or $costweaveTrimmed.StartsWith("#")) {
        continue
    }
    $costweaveSeparator = $costweaveTrimmed.IndexOf("=")
    if ($costweaveSeparator -lt 1) {
        throw "Invalid environment entry in ${EnvironmentFile}: $costweaveLine"
    }
    $costweaveName = $costweaveTrimmed.Substring(0, $costweaveSeparator).Trim()
    if ($costweaveName -notmatch '^[A-Z][A-Z0-9_]*$') {
        throw "Invalid environment variable name in ${EnvironmentFile}: $costweaveName"
    }
    $costweaveValue = $costweaveTrimmed.Substring($costweaveSeparator + 1).Trim()
    if (
        $costweaveValue.Length -ge 2 -and
        (($costweaveValue.StartsWith('"') -and $costweaveValue.EndsWith('"')) -or
         ($costweaveValue.StartsWith("'") -and $costweaveValue.EndsWith("'")))
    ) {
        $costweaveValue = $costweaveValue.Substring(1, $costweaveValue.Length - 2)
    }
    [Environment]::SetEnvironmentVariable($costweaveName, $costweaveValue, "Process")
}

foreach ($costweavePathName in @("COSTWEAVE_CATALOG_PATH", "COSTWEAVE_RUNTIME_DB")) {
    $costweaveConfiguredPath = [Environment]::GetEnvironmentVariable($costweavePathName, "Process")
    if ([string]::IsNullOrWhiteSpace($costweaveConfiguredPath)) {
        throw "$costweavePathName must be configured in $EnvironmentFile"
    }
    if (-not [IO.Path]::IsPathRooted($costweaveConfiguredPath)) {
        $costweaveConfiguredPath = [IO.Path]::GetFullPath(
            (Join-Path $script:CostWeaveProjectRoot $costweaveConfiguredPath)
        )
        [Environment]::SetEnvironmentVariable(
            $costweavePathName,
            $costweaveConfiguredPath,
            "Process"
        )
    }
    $costweaveParent = Split-Path -Parent $costweaveConfiguredPath
    New-Item -ItemType Directory -Path $costweaveParent -Force | Out-Null
}

Set-Location -LiteralPath $script:CostWeaveProjectRoot
