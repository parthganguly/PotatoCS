param(
    [string]$ModelDir = "",
    [switch]$AllowMissing
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $ModelDir) {
    $ModelDir = Join-Path $repoRoot "models\florence2-base-ft"
}

$manifestPath = Join-Path $ModelDir "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    $legacyManifest = Join-Path $ModelDir "odysseus-florence2-manifest.json"
    if (Test-Path -LiteralPath $legacyManifest) {
        $manifestPath = $legacyManifest
    }
}
if (-not (Test-Path -LiteralPath $manifestPath)) {
    if ($AllowMissing) {
        Write-Host "Florence-2 model pack is not installed at $ModelDir"
        exit 0
    }
    throw "Florence-2 model pack manifest is missing: $manifestPath"
}

$manifestJson = [System.IO.File]::ReadAllText($manifestPath, [System.Text.Encoding]::UTF8)
if ($manifestJson.Length -gt 0 -and $manifestJson[0] -eq [char]0xFEFF) {
    $manifestJson = $manifestJson.Substring(1)
}
if (-not $manifestJson.Trim()) {
    throw "Florence-2 manifest is empty: $manifestPath"
}
$manifest = $manifestJson | ConvertFrom-Json
if ($manifest.pack_id -ne "florence2-base-ft") {
    throw "Florence-2 manifest pack_id mismatch."
}
if ($manifest.model_id -ne "microsoft/Florence-2-base-ft") {
    throw "Florence-2 manifest model_id mismatch."
}
if ($manifest.revision -ne "f6c1a25888ffc1d945ee8a1a77ac833c7303d46e") {
    throw "Florence-2 manifest revision mismatch."
}
if ($manifest.trust_remote_code -ne $false) {
    throw "Florence-2 manifest must record trust_remote_code=false."
}

$required = @("LICENSE", "README.md", "config.json", "model.safetensors", "preprocessor_config.json", "tokenizer.json", "tokenizer_config.json", "vocab.json")
foreach ($name in $required) {
    $entry = $manifest.files.$name
    if (-not $entry) {
        throw "Florence-2 manifest is missing file entry: $name"
    }
    $path = Join-Path $ModelDir $name
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Florence-2 file is missing: $name"
    }
    $item = Get-Item -LiteralPath $path
    if ($item.Length -ne [int64]$entry.size_bytes) {
        throw "Florence-2 file size mismatch for $name. Expected $($entry.size_bytes), got $($item.Length)."
    }
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne ([string]$entry.sha256).ToLowerInvariant()) {
        throw "Florence-2 checksum mismatch for $name."
    }
}

Write-Host "Florence-2 model pack verified at $ModelDir"
