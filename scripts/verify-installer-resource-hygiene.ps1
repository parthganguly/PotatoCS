param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Core", "Florence")]
    [string]$Variant,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$InstallerPath = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Tauri resource configuration is missing: $ConfigPath"
}

$resolvedConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$configDir = Split-Path -Parent $resolvedConfigPath
$config = Get-Content -LiteralPath $resolvedConfigPath -Raw | ConvertFrom-Json
$resources = $config.bundle.resources
if (-not $resources) {
    throw "Tauri resource configuration has no bundle.resources mapping: $resolvedConfigPath"
}

$listing = [System.Collections.Generic.List[string]]::new()
foreach ($resource in $resources.PSObject.Properties) {
    $source = [string]$resource.Name
    $destination = [string]$resource.Value
    $listing.Add("resource $source -> $destination")

    $sourcePath = [System.IO.Path]::GetFullPath((Join-Path $configDir $source))
    if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
        $listing.Add($destination)
    } elseif (Test-Path -LiteralPath $sourcePath -PathType Container) {
        $sourcePrefixLength = $sourcePath.TrimEnd("\", "/").Length
        foreach ($file in Get-ChildItem -LiteralPath $sourcePath -File -Recurse) {
            $relativePath = $file.FullName.Substring($sourcePrefixLength).TrimStart("\", "/")
            $listing.Add((Join-Path $destination $relativePath))
        }
    }
}

if ($InstallerPath) {
    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
        throw "Installer is missing: $InstallerPath"
    }

    $sevenZip = (Get-Command 7z -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if (-not $sevenZip) {
        throw "7z is required to inspect the NSIS installer contents."
    }

    $installerListing = @(& $sevenZip l -slt $InstallerPath)
    if ($LASTEXITCODE -ne 0) {
        throw "7z failed to inspect installer: $InstallerPath"
    }
    foreach ($line in $installerListing) {
        $listing.Add([string]$line)
    }
}

$patterns = [ordered]@{
    evals = '(?i)(^|[\\/\s])evals([\\/]|$)'
    benchmarks = '(?i)(^|[\\/\s])benchmarks([\\/]|$)'
    reports = '(?i)(^|[\\/\s])reports([\\/]|$)'
    private_temp = '(?i)(^|[\\/\s])(?:tmp-real-vicky[^\\/\s]*|\.tmp-webview2[^\\/\s]*)([\\/]|$)'
    login_data = '(?i)(^|[\\/\s])Login Data(?:-journal)?(?=$|[\\/\s])'
    cookies = '(?i)(^|[\\/\s])Cookies(?=$|[\\/\s])'
    sqlite = '(?i)\.sqlite3?(?=$|[\\/\s])'
    db = '(?i)\.db(?=$|[\\/\s])'
}

if ($Variant -eq "Core") {
    $patterns["florence"] = '(?i)(^|[\\/\s])models[\\/]florence2-base-ft([\\/]|$)'
    $patterns["model_safetensors"] = '(?i)(^|[\\/\s])model\.safetensors(?=$|[\\/\s])'
    $patterns["torch"] = '(?i)(^|[\\/\s])torch(?:vision)?(?:-[^\\/\s]+)?([\\/]|$)'
    $patterns["transformers"] = '(?i)(^|[\\/\s])transformers(?:-[^\\/\s]+)?([\\/]|$)'
}

$violations = [System.Collections.Generic.List[string]]::new()
foreach ($entry in $patterns.GetEnumerator()) {
    $matches = @($listing | Where-Object { $_ -match $entry.Value })
    Write-Host ("hygiene_{0}={1}" -f $entry.Key, $matches.Count)
    if ($matches.Count -gt 0) {
        $sample = ($matches | Select-Object -First 3) -join "; "
        $violations.Add(("{0} ({1}): {2}" -f $entry.Key, $matches.Count, $sample))
    }
}

if ($violations.Count -gt 0) {
    throw ("Forbidden packaged resources found for {0}: {1}" -f $Variant, ($violations -join " | "))
}

Write-Host "Installer resource hygiene verified for $Variant."
