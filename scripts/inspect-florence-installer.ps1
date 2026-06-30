param(
    [string]$InstallerPath = "",
    [string]$ResourceRoot = "",
    [string]$RuntimeDir = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $InstallerPath) {
    $InstallerPath = Join-Path $repoRoot "src-tauri\target\release\bundle\nsis\Odysseus Desktop_0.2.0_x64-setup.exe"
}
if (-not $ResourceRoot) {
    $ResourceRoot = Join-Path $repoRoot "src-tauri\generated-resources"
}
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $repoRoot "python-runtime-florence"
}

if (-not (Test-Path -LiteralPath $InstallerPath)) {
    throw "Installer is missing: $InstallerPath"
}

$sevenZip = (Get-Command 7z -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $sevenZip) {
    throw "7z is required to inspect the NSIS installer contents."
}

$florenceConfig = Join-Path $repoRoot "src-tauri\tauri.florence.conf.json"
& (Join-Path $PSScriptRoot "verify-installer-resource-hygiene.ps1") -Variant Florence -ConfigPath $florenceConfig -InstallerPath $InstallerPath

$required = @(
    "models\florence2-base-ft\LICENSE",
    "models\florence2-base-ft\README.md",
    "models\florence2-base-ft\config.json",
    "models\florence2-base-ft\manifest.json",
    "models\florence2-base-ft\model.safetensors",
    "models\florence2-base-ft\preprocessor_config.json",
    "models\florence2-base-ft\tokenizer.json",
    "models\florence2-base-ft\tokenizer_config.json",
    "models\florence2-base-ft\vocab.json"
)

$listing = & $sevenZip l $InstallerPath
if ($LASTEXITCODE -ne 0) {
    throw "7z failed to inspect installer: $InstallerPath"
}

$normalizedListing = $listing -replace "/", "\"
foreach ($path in $required) {
    if (-not ($normalizedListing | Select-String -Pattern ([Regex]::Escape($path)))) {
        throw "Installer does not contain required Florence resource: $path"
    }
}

$florenceLines = @($normalizedListing | Where-Object { $_ -match "models\\florence2-base-ft\\" })
$florenceListedBytes = 0L
foreach ($line in $florenceLines) {
    if ($line -match "^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+(\d+)") {
        $florenceListedBytes += [int64]$matches[1]
    }
}

$packDir = Join-Path $ResourceRoot "models\florence2-base-ft"
$expectedFlorenceBytes = if (Test-Path -LiteralPath $packDir) {
    (Get-ChildItem -LiteralPath $packDir -File -Recurse | Measure-Object -Property Length -Sum).Sum
} else {
    0
}
$runtimeBytes = if (Test-Path -LiteralPath $RuntimeDir) {
    (Get-ChildItem -LiteralPath $RuntimeDir -File -Recurse | Measure-Object -Property Length -Sum).Sum
} else {
    0
}
$installer = Get-Item -LiteralPath $InstallerPath

[pscustomobject]@{
    installer_path = $installer.FullName
    installer_bytes = $installer.Length
    installer_last_write_time = $installer.LastWriteTime
    florence_entries = $florenceLines.Count
    florence_listed_bytes = $florenceListedBytes
    expected_installed_florence_bytes = $expectedFlorenceBytes
    runtime_bytes = $runtimeBytes
    compressed_contribution = "not reported per file by NSIS/7z listing"
} | ConvertTo-Json -Depth 4
