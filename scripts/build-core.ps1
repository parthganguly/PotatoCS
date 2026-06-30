param(
    [switch]$SkipTauriBuild
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$tauri = Join-Path $repoRoot "node_modules\.bin\tauri.cmd"
$runtimeDir = Join-Path $repoRoot "python-runtime-core"
$coreConfig = Join-Path $repoRoot "src-tauri\tauri.core.conf.json"
$tauriConfig = Join-Path $repoRoot "src-tauri\tauri.conf.json"
$tauriSettings = Get-Content -LiteralPath $tauriConfig -Raw | ConvertFrom-Json
$installerName = "{0}_{1}_x64-setup.exe" -f $tauriSettings.productName, $tauriSettings.version
$installerPath = Join-Path $repoRoot "src-tauri\target\release\bundle\nsis\$installerName"

if (-not (Test-Path -LiteralPath $tauri)) {
    throw "Local Tauri CLI is missing. Run npm install first."
}
if (-not (Test-Path -LiteralPath $coreConfig)) {
    throw "Core Tauri resource configuration is missing: $coreConfig"
}

$oldIncludeFlorence = $env:ODYSSEUS_INCLUDE_FLORENCE
try {
    Remove-Item Env:\ODYSSEUS_INCLUDE_FLORENCE -ErrorAction SilentlyContinue

    & (Join-Path $PSScriptRoot "prepare-python-runtime.ps1") -RuntimeDir $runtimeDir -Clean
    if ($LASTEXITCODE -ne 0) {
        throw "Core Python runtime preparation failed."
    }

    & (Join-Path $PSScriptRoot "verify-python-runtime.ps1") -RuntimeDir $runtimeDir
    if ($LASTEXITCODE -ne 0) {
        throw "Core Python runtime verification failed."
    }

    & (Join-Path $PSScriptRoot "verify-installer-resource-hygiene.ps1") -Variant Core -ConfigPath $coreConfig

    if (-not $SkipTauriBuild) {
        & $tauri build --config $coreConfig
        if ($LASTEXITCODE -ne 0) {
            throw "Core Tauri release build failed."
        }
        & (Join-Path $PSScriptRoot "verify-installer-resource-hygiene.ps1") -Variant Core -ConfigPath $coreConfig -InstallerPath $installerPath
    }
} finally {
    $env:ODYSSEUS_INCLUDE_FLORENCE = $oldIncludeFlorence
}
