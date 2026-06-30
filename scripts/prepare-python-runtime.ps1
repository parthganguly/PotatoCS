param(
    [string]$PythonVersion = "3.12.8",
    [string]$RuntimeDir = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $repoRoot "python-runtime"
}
$runtimeDir = if ([System.IO.Path]::IsPathRooted($RuntimeDir)) {
    $RuntimeDir
} else {
    Join-Path $repoRoot $RuntimeDir
}
$runtimeDir = [System.IO.Path]::GetFullPath($runtimeDir)
if (-not $runtimeDir.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to stage Python runtime outside the repository: $runtimeDir"
}
$requirements = Join-Path $repoRoot "python\requirements.txt"
$florenceRequirements = Join-Path $repoRoot "python\requirements-florence.txt"
$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipPath = Join-Path $env:TEMP $zipName
$url = "https://www.python.org/ftp/python/$PythonVersion/$zipName"

if ($Clean -and (Test-Path -LiteralPath $runtimeDir)) {
    Write-Host "Removing existing staged Python runtime at $runtimeDir"
    Remove-Item -LiteralPath $runtimeDir -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir "python.exe"))) {
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $zipPath
    Expand-Archive -LiteralPath $zipPath -DestinationPath $runtimeDir -Force
}

$pth = Get-ChildItem -LiteralPath $runtimeDir -Filter "python*._pth" | Select-Object -First 1
if ($pth) {
    $content = Get-Content -LiteralPath $pth.FullName
    if ($content -notcontains "import site") {
        $content = $content | ForEach-Object {
            if ($_ -eq "#import site") { "import site" } else { $_ }
        }
        Set-Content -LiteralPath $pth.FullName -Value $content -Encoding ASCII
    }
}

$pythonExe = Join-Path $runtimeDir "python.exe"
$pipExe = Join-Path $runtimeDir "Scripts\pip.exe"
if (-not (Test-Path -LiteralPath $pipExe)) {
    $getPip = Join-Path $env:TEMP "get-pip.py"
    Write-Host "Installing pip into embedded runtime"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
    & $pythonExe $getPip
}

if (Test-Path -LiteralPath $requirements) {
    Write-Host "Installing Python backend requirements"
    & $pythonExe -m pip install --disable-pip-version-check --upgrade -r $requirements
}

if ($env:ODYSSEUS_INCLUDE_FLORENCE -eq "1") {
    if (-not (Test-Path -LiteralPath $florenceRequirements)) {
        throw "Florence requirements file is missing: $florenceRequirements"
    }
    Write-Host "Installing optional Florence-2 runtime requirements"
    & $pythonExe -m pip install --disable-pip-version-check --upgrade -r $florenceRequirements
    & (Join-Path $PSScriptRoot "prepare-florence2-model.ps1")
    & (Join-Path $PSScriptRoot "verify-florence2-model.ps1")
} else {
    Write-Host "Florence-2 optional runtime not staged. Set ODYSSEUS_INCLUDE_FLORENCE=1 to include it."
}

# Dependency wheels can include development-only benchmark suites. They are not
# runtime inputs and must never be copied into an end-user installer.
$sitePackages = Join-Path $runtimeDir "Lib\site-packages"
if (Test-Path -LiteralPath $sitePackages -PathType Container) {
    $packagedBenchmarkDirs = @(
        Get-ChildItem -LiteralPath $sitePackages -Directory -Recurse -Filter "benchmarks" |
            Sort-Object { $_.FullName.Length } -Descending
    )
    foreach ($benchmarkDir in $packagedBenchmarkDirs) {
        Write-Host "Removing development-only benchmark directory $($benchmarkDir.FullName)"
        Remove-Item -LiteralPath $benchmarkDir.FullName -Recurse -Force
    }
}

$backendPath = Join-Path $repoRoot "python"
$verify = @"
import importlib
import os
import sys
sys.path.insert(0, os.environ["ODYSSEUS_BACKEND_PATH"])
for module in ("json", "sqlite3", "numpy", "pypdf", "reportlab", "PIL", "rpc_server", "odysseus_desktop_backend"):
    importlib.import_module(module)
print("embedded Python runtime verified")
"@
$oldPythonPath = $env:PYTHONPATH
$oldBackendPath = $env:ODYSSEUS_BACKEND_PATH
$verifyPath = Join-Path $env:TEMP "odysseus-verify-python-runtime.py"
try {
    $env:PYTHONPATH = $backendPath
    $env:ODYSSEUS_BACKEND_PATH = $backendPath
    Set-Content -LiteralPath $verifyPath -Value $verify -Encoding UTF8
    & $pythonExe $verifyPath
    if ($LASTEXITCODE -ne 0) {
        throw "Embedded Python verification failed."
    }
} finally {
    $env:PYTHONPATH = $oldPythonPath
    $env:ODYSSEUS_BACKEND_PATH = $oldBackendPath
    Remove-Item -LiteralPath $verifyPath -Force -ErrorAction SilentlyContinue
}

Write-Host "Python runtime staged at $runtimeDir"
