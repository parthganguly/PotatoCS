param(
    [ValidateSet("T", "S", "all")]
    [string]$Mode = "all"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    python scripts\search_fixture_proof.py --mode $Mode
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($Mode -eq "T") {
        python -m pytest python\tests\test_search_v0.py -k "fixture_end_to_end_search" -v
    } elseif ($Mode -eq "S") {
        python -m pytest python\tests\test_search_v0.py -k "one_bounded_second_round" -v
    } else {
        python -m pytest python\tests\test_search_v0.py -v
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
