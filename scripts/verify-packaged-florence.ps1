param(
    [string]$ResourceRoot = "",
    [string]$RuntimeDir = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $ResourceRoot) {
    $ResourceRoot = Join-Path $repoRoot "src-tauri\generated-resources"
}
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $repoRoot "python-runtime"
}

$packDir = Join-Path $ResourceRoot "models\florence2-base-ft"
$manifestPath = Join-Path $packDir "manifest.json"
$pythonExe = Join-Path $RuntimeDir "python.exe"
$florenceConfig = Join-Path $repoRoot "src-tauri\tauri.florence.conf.json"
$requiredFiles = @(
    "LICENSE",
    "README.md",
    "config.json",
    "manifest.json",
    "model.safetensors",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json"
)

if (-not (Test-Path -LiteralPath $packDir)) {
    throw "Packaged Florence resource directory is missing: $packDir"
}
foreach ($name in $requiredFiles) {
    $path = Join-Path $packDir $name
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Packaged Florence file is missing: $name"
    }
}
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Packaged Python runtime is missing: $pythonExe"
}

& (Join-Path $PSScriptRoot "verify-florence2-model.ps1") -ModelDir $packDir

& (Join-Path $PSScriptRoot "verify-installer-resource-hygiene.ps1") -Variant Florence -ConfigPath $florenceConfig

$modelCopies = @(Get-ChildItem -LiteralPath $ResourceRoot -Recurse -File -Filter "model.safetensors")
if ($modelCopies.Count -ne 1) {
    throw "Expected exactly one packaged Florence model.safetensors under $ResourceRoot, found $($modelCopies.Count)."
}

$textFiles = @("manifest.json", "config.json", "preprocessor_config.json", "tokenizer_config.json", "README.md", "LICENSE")
foreach ($name in $textFiles) {
    $path = Join-Path $packDir $name
    $matches = Select-String -LiteralPath $path -Pattern $repoRoot -SimpleMatch -ErrorAction SilentlyContinue
    if ($matches) {
        throw "Packaged Florence file embeds the source repository path: $name"
    }
}

$verify = @"
import importlib
import importlib.metadata
from transformers import AutoProcessor, Florence2ForConditionalGeneration

expected = {
    "torch": "2.12.0",
    "torchvision": "0.27.0",
    "transformers": "5.12.1",
    "safetensors": "0.8.0",
    "tokenizers": "0.22.2",
    "huggingface_hub": "1.19.0",
}
for module_name, expected_version in expected.items():
    importlib.import_module(module_name)
    actual = importlib.metadata.version(module_name)
    if tuple(int(part) for part in actual.split(".")[:3]) < tuple(int(part) for part in expected_version.split(".")[:3]):
        raise SystemExit(f"{module_name} is too old: {actual} < {expected_version}")
importlib.import_module("PIL")
print("packaged-florence-runtime-ok")
"@

$verifyPath = Join-Path $env:TEMP "odysseus-verify-packaged-florence-runtime.py"
$oldOffline = $env:HF_HUB_OFFLINE
$oldTransformersOffline = $env:TRANSFORMERS_OFFLINE
try {
    $env:HF_HUB_OFFLINE = "1"
    $env:TRANSFORMERS_OFFLINE = "1"
    Set-Content -LiteralPath $verifyPath -Value $verify -Encoding UTF8
    & $pythonExe $verifyPath
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged Florence runtime import verification failed."
    }
} finally {
    $env:HF_HUB_OFFLINE = $oldOffline
    $env:TRANSFORMERS_OFFLINE = $oldTransformersOffline
    Remove-Item -LiteralPath $verifyPath -Force -ErrorAction SilentlyContinue
}

$packBytes = (Get-ChildItem -LiteralPath $packDir -File -Recurse | Measure-Object -Property Length -Sum).Sum
$runtimeBytes = (Get-ChildItem -LiteralPath $RuntimeDir -File -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Host "Packaged Florence resources verified at $packDir"
Write-Host "Packaged Florence bytes: $packBytes"
Write-Host "Packaged Python runtime bytes: $runtimeBytes"
