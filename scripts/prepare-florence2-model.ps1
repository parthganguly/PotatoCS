param(
    [string]$OutputDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $OutputDir) {
    $OutputDir = Join-Path $repoRoot "models\florence2-base-ft"
}

$modelId = "microsoft/Florence-2-base-ft"
$revision = "f6c1a25888ffc1d945ee8a1a77ac833c7303d46e"
$manifestName = "manifest.json"
$files = @(
    @{ Name = "LICENSE"; Size = 1141 },
    @{ Name = "README.md"; Size = 14820 },
    @{ Name = "config.json"; Size = 2430 },
    @{ Name = "model.safetensors"; Size = 463221266 },
    @{ Name = "preprocessor_config.json"; Size = 806 },
    @{ Name = "tokenizer.json"; Size = 1355863 },
    @{ Name = "tokenizer_config.json"; Size = 34 },
    @{ Name = "vocab.json"; Size = 1099884 }
)

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$manifestFiles = [ordered]@{}
foreach ($file in $files) {
    $name = [string]$file.Name
    $target = Join-Path $OutputDir $name
    if ($Force -or -not (Test-Path -LiteralPath $target)) {
        $url = "https://huggingface.co/$modelId/resolve/$revision/$name"
        Write-Host "Downloading $name"
        Invoke-WebRequest -Uri $url -OutFile $target
    }
    $item = Get-Item -LiteralPath $target
    if ($item.Length -ne [int64]$file.Size) {
        throw "Downloaded Florence-2 file size mismatch for $name. Expected $($file.Size), got $($item.Length)."
    }
    $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifestFiles[$name] = [ordered]@{
        size_bytes = [int64]$item.Length
        sha256 = $hash
    }
}

$manifest = [ordered]@{
    pack_id = "florence2-base-ft"
    model_id = $modelId
    revision = $revision
    license = "MIT"
    trust_remote_code = $false
    normal_runtime_downloads = $false
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    files = $manifestFiles
}
$manifestPath = Join-Path $OutputDir $manifestName
$manifestJson = $manifest | ConvertTo-Json -Depth 5
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8NoBom)
Write-Host "Florence-2 model pack prepared at $OutputDir"
