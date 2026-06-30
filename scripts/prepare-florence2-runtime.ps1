param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$requirements = Join-Path $repoRoot "python\requirements-florence.txt"

if (-not $PythonExe) {
    if ($env:ODYSSEUS_PYTHON) {
        $PythonExe = $env:ODYSSEUS_PYTHON
    } else {
        $embedded = Join-Path $repoRoot "python-runtime\python.exe"
        if (Test-Path -LiteralPath $embedded) {
            $PythonExe = $embedded
        } else {
            $command = Get-Command python -ErrorAction Stop
            $PythonExe = $command.Source
        }
    }
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python interpreter not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $requirements)) {
    throw "Florence requirements file is missing: $requirements"
}

$resolvedPython = (& $PythonExe -c "import sys; print(sys.executable)").Trim()
Write-Host "Preparing Florence-2 runtime in Python: $resolvedPython"
& $PythonExe -m pip install --disable-pip-version-check --upgrade -r $requirements

$verify = @"
import sys
print("python_executable=" + sys.executable)
import torch
import torchvision
import transformers
import safetensors
import tokenizers
from transformers import AutoImageProcessor, AutoTokenizer, Florence2ForConditionalGeneration
from transformers.models.florence2.processing_florence2 import Florence2Processor
print("torch=" + torch.__version__)
print("torchvision=" + torchvision.__version__)
print("transformers=" + transformers.__version__)
print("safetensors=" + safetensors.__version__)
print("tokenizers=" + tokenizers.__version__)
print("florence_native_classes=ok")
"@
$verifyPath = Join-Path $env:TEMP "odysseus-verify-florence-runtime.py"
try {
    [System.IO.File]::WriteAllText($verifyPath, $verify, [System.Text.UTF8Encoding]::new($false))
    & $PythonExe $verifyPath
    if ($LASTEXITCODE -ne 0) {
        throw "Florence runtime import verification failed."
    }
} finally {
    Remove-Item -LiteralPath $verifyPath -Force -ErrorAction SilentlyContinue
}
