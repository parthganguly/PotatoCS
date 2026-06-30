param(
    [switch]$SkipTauriBuild
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$tauri = Join-Path $repoRoot "node_modules\.bin\tauri.cmd"
$runtimeDir = Join-Path $repoRoot "python-runtime-florence"
$florenceConfig = Join-Path $repoRoot "src-tauri\tauri.florence.conf.json"
if (-not (Test-Path -LiteralPath $tauri)) {
    throw "Local Tauri CLI is missing. Run npm install first."
}
if (-not (Test-Path -LiteralPath $florenceConfig)) {
    throw "Florence Tauri resource configuration is missing: $florenceConfig"
}

$oldIncludeFlorence = $env:ODYSSEUS_INCLUDE_FLORENCE
try {
    $env:ODYSSEUS_INCLUDE_FLORENCE = "1"
    & (Join-Path $PSScriptRoot "prepare-python-runtime.ps1") -RuntimeDir $runtimeDir -Clean
    if ($LASTEXITCODE -ne 0) {
        throw "Python runtime preparation failed."
    }

    & (Join-Path $PSScriptRoot "verify-python-runtime.ps1") -RuntimeDir $runtimeDir
    if ($LASTEXITCODE -ne 0) {
        throw "Python runtime verification failed."
    }

    & (Join-Path $PSScriptRoot "verify-florence2-model.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Source Florence pack verification failed."
    }

    & (Join-Path $PSScriptRoot "stage-florence2-resources.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Florence resource staging failed."
    }

    & (Join-Path $PSScriptRoot "verify-packaged-florence.ps1") -RuntimeDir $runtimeDir
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged Florence verification failed."
    }

    if (-not $SkipTauriBuild) {
        & $tauri build --config $florenceConfig
        if ($LASTEXITCODE -ne 0) {
            throw "Tauri release build failed."
        }
        & (Join-Path $PSScriptRoot "inspect-florence-installer.ps1") -RuntimeDir $runtimeDir
        if ($LASTEXITCODE -ne 0) {
            throw "Florence installer inspection failed."
        }
    }
} finally {
    $env:ODYSSEUS_INCLUDE_FLORENCE = $oldIncludeFlorence
}
