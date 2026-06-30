param(
    [string]$SourceModelDir = "",
    [string]$OutputRoot = "",
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $SourceModelDir) {
    $SourceModelDir = Join-Path $repoRoot "models\florence2-base-ft"
}
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $repoRoot "src-tauri\generated-resources"
}

$SourceModelDir = (Resolve-Path -LiteralPath $SourceModelDir).Path
$outputPack = Join-Path $OutputRoot "models\florence2-base-ft"
$required = @(
    "LICENSE",
    "README.md",
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json"
)

if (-not $SkipVerify) {
    & (Join-Path $PSScriptRoot "verify-florence2-model.ps1") -ModelDir $SourceModelDir
}

$manifestPath = Join-Path $SourceModelDir "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    $legacyManifest = Join-Path $SourceModelDir "odysseus-florence2-manifest.json"
    if (Test-Path -LiteralPath $legacyManifest) {
        $manifestPath = $legacyManifest
    }
}
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "Florence-2 source manifest is missing in $SourceModelDir"
}

$manifestJson = [System.IO.File]::ReadAllText($manifestPath, [System.Text.Encoding]::UTF8)
if ($manifestJson.Length -gt 0 -and $manifestJson[0] -eq [char]0xFEFF) {
    $manifestJson = $manifestJson.Substring(1)
}
$manifest = $manifestJson | ConvertFrom-Json

$manifestFiles = @($manifest.files.PSObject.Properties.Name)
$unexpected = @($manifestFiles | Where-Object { $_ -notin $required })
if ($unexpected.Count -gt 0) {
    throw "Florence-2 manifest contains unexpected files: $($unexpected -join ', ')"
}

foreach ($name in $required) {
    $entry = $manifest.files.$name
    if (-not $entry) {
        throw "Florence-2 manifest is missing required file: $name"
    }
    $sourcePath = Join-Path $SourceModelDir $name
    if (-not (Test-Path -LiteralPath $sourcePath)) {
        throw "Florence-2 source file is missing: $name"
    }
    $item = Get-Item -LiteralPath $sourcePath
    if ($item.Length -ne [int64]$entry.size_bytes) {
        throw "Florence-2 source size mismatch for $name. Expected $($entry.size_bytes), got $($item.Length)."
    }
    $hash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne ([string]$entry.sha256).ToLowerInvariant()) {
        throw "Florence-2 source checksum mismatch for $name."
    }
}

if (Test-Path -LiteralPath $OutputRoot) {
    Remove-Item -LiteralPath $OutputRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $outputPack | Out-Null

foreach ($name in ($required | Sort-Object)) {
    Copy-Item -LiteralPath (Join-Path $SourceModelDir $name) -Destination (Join-Path $outputPack $name)
}

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText((Join-Path $outputPack "manifest.json"), ($manifestJson.Trim() + "`n"), $utf8NoBom)

& (Join-Path $PSScriptRoot "verify-florence2-model.ps1") -ModelDir $outputPack

$resourceBytes = (Get-ChildItem -LiteralPath $outputPack -File -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Host "Florence-2 resources staged at $outputPack"
Write-Host "Florence-2 staged bytes: $resourceBytes"
