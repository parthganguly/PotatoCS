param(
    [switch]$SkipRuntime
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $repoRoot
try {
    if (-not $SkipRuntime) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prepare-python-runtime.ps1
        & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-python-runtime.ps1
    }

    python -m pytest python/tests
    python -m py_compile `
        python/odysseus_desktop_backend/services/artifact_service.py `
        python/odysseus_desktop_backend/services/vision_service.py `
        python/odysseus_desktop_backend/services/image_eval_service.py `
        python/odysseus_desktop_backend/services/ocr_service.py `
        python/odysseus_desktop_backend/services/model_service.py `
        python/odysseus_desktop_backend/services/chat_service.py `
        python/rpc_server.py
    npm run build

    $cargo = Get-Command cargo -ErrorAction SilentlyContinue
    if (-not $cargo) {
        $cargoPath = Join-Path $env:USERPROFILE ".cargo\bin"
        if (Test-Path -LiteralPath (Join-Path $cargoPath "cargo.exe")) {
            $env:PATH = "$cargoPath;$env:PATH"
            $cargo = Get-Command cargo -ErrorAction SilentlyContinue
        }
    }

    if ($cargo) {
        Push-Location src-tauri
        try {
            cargo check
        } finally {
            Pop-Location
        }
    } else {
        Write-Warning "cargo was not found; skipped Rust cargo check."
    }

    Write-Host "MVP smoke checks passed."
} finally {
    Pop-Location
}
