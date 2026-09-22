$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    python scripts/search_v1_fixture_proof.py
    python -m pytest python/tests/test_search_v1_local_discovery.py -q
}
finally {
    Pop-Location
}
