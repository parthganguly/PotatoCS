[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceRoot,

    [Parameter(Mandatory = $true)]
    [string]$BuildRoot,

    [string]$Msys2Root = 'C:\msys64'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$PinnedCommit = '72d3d37231e922a6fa9afca16e08fa45842d5eb4'
$ExpectedFixtureSha256 = '5c80caf2fa4a3f22f1497e0eacacf9025d28d5c2ece191cc4a0e966c049768dc'
$ExpectedStdout = [Text.Encoding]::ASCII.GetBytes(
    "idot kernel exactness (avx2): ok`r`nidot driver exactness (avx2): ok`r`n"
)
$MaxStreamBytes = 4096
$TimeoutMilliseconds = 30000

function Invoke-CheckedGit {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw 'git command failed'
    }
}

function Get-Sha256 {
    param([string]$LiteralPath)

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $LiteralPath).Hash.ToLowerInvariant()
}

function Test-ExactBytes {
    param([byte[]]$Actual, [byte[]]$Expected)

    if ($Actual.Length -ne $Expected.Length) {
        return $false
    }
    for ($index = 0; $index -lt $Expected.Length; $index++) {
        if ($Actual[$index] -ne $Expected[$index]) {
            return $false
        }
    }
    return $true
}

function Invoke-BoundedOracle {
    param([string]$Executable, [string]$WorkingDirectory)

    $stdoutFile = Join-Path $WorkingDirectory 'repro-oracle.stdout.bin'
    $stderrFile = Join-Path $WorkingDirectory 'repro-oracle.stderr.bin'
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Executable
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw 'oracle could not be started'
    }

    $stdoutStream = [IO.File]::Create($stdoutFile)
    $stderrStream = [IO.File]::Create($stderrFile)
    $stdoutCopy = $process.StandardOutput.BaseStream.CopyToAsync($stdoutStream)
    $stderrCopy = $process.StandardError.BaseStream.CopyToAsync($stderrStream)
    $timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        while (-not $process.WaitForExit(10)) {
            $stdoutStream.Flush()
            $stderrStream.Flush()
            if ($stdoutStream.Length -gt $MaxStreamBytes -or
                $stderrStream.Length -gt $MaxStreamBytes) {
                $process.Kill()
                throw 'oracle output exceeded the fixed limit'
            }
            if ($timer.ElapsedMilliseconds -ge $TimeoutMilliseconds) {
                $process.Kill()
                throw 'oracle exceeded the fixed time limit'
            }
        }
        [void]$stdoutCopy.GetAwaiter().GetResult()
        [void]$stderrCopy.GetAwaiter().GetResult()
        $stdoutStream.Flush()
        $stderrStream.Flush()
        $exitCode = $process.ExitCode
    } finally {
        $stdoutStream.Dispose()
        $stderrStream.Dispose()
        $process.Dispose()
    }

    $stdout = [IO.File]::ReadAllBytes($stdoutFile)
    $stderr = [IO.File]::ReadAllBytes($stderrFile)
    if ($stdout.Length -gt $MaxStreamBytes -or $stderr.Length -gt $MaxStreamBytes) {
        throw 'oracle output exceeded the fixed limit'
    }
    if ($exitCode -ne 0) {
        throw 'oracle exited unsuccessfully'
    }
    if ($stderr.Length -ne 0) {
        throw 'oracle wrote unexpected stderr'
    }
    if (-not (Test-ExactBytes -Actual $stdout -Expected $ExpectedStdout)) {
        throw 'oracle stdout did not match the exact 68-byte AVX2 result'
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        StdoutBytes = $stdout.Length
        StderrBytes = $stderr.Length
    }
}

$launcher = Join-Path $Msys2Root 'msys2_shell.cmd'
$gcc = Join-Path $Msys2Root 'ucrt64\bin\gcc.exe'
$make = Join-Path $Msys2Root 'usr\bin\make.exe'
foreach ($requiredFile in @($launcher, $gcc, $make)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw 'required MSYS2 UCRT64 tool is unavailable'
    }
}

$resolvedSource = (Resolve-Path -LiteralPath $SourceRoot).Path
$resolvedCommit = (& git -C $resolvedSource rev-parse "$PinnedCommit^{commit}").Trim()
if ($LASTEXITCODE -ne 0 -or $resolvedCommit -ne $PinnedCommit) {
    throw 'pinned Colibri commit is unavailable in the source repository'
}
$sourceDateEpoch = (& git -C $resolvedSource show -s --format=%ct $PinnedCommit).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceDateEpoch -notmatch '^\d+$') {
    throw 'pinned Colibri commit epoch is unavailable'
}

New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
$resolvedBuildRoot = (Resolve-Path -LiteralPath $BuildRoot).Path
$builds = @()
foreach ($label in @('build-a', 'build-b')) {
    $target = Join-Path $resolvedBuildRoot $label
    if (Test-Path -LiteralPath $target) {
        throw 'clean build target already exists'
    }
    Invoke-CheckedGit clone --quiet --no-checkout -- $resolvedSource $target
    Invoke-CheckedGit -C $target checkout --quiet --detach $PinnedCommit

    $cRoot = Join-Path $target 'c'
    Push-Location $cRoot
    try {
        $env:SOURCE_DATE_EPOCH = $sourceDateEpoch
        & $launcher -defterm -here -no-start -ucrt64 -c `
            'make glm.exe ARCH=x86-64-v3 && make tests/test_idot.exe ARCH=x86-64-v3'
        if ($LASTEXITCODE -ne 0) {
            throw 'native build failed'
        }
    } finally {
        Remove-Item Env:SOURCE_DATE_EPOCH -ErrorAction SilentlyContinue
        Pop-Location
    }

    $engine = Join-Path $cRoot 'glm.exe'
    $oracle = Join-Path $cRoot 'tests\test_idot.exe'
    $fixture = Join-Path $cRoot 'tests\test_idot.c'
    $run = @(Invoke-BoundedOracle -Executable $oracle -WorkingDirectory (Split-Path $oracle))
    if ($run.Count -ne 1 -or $run[0].PSObject.Properties.Name -notcontains 'ExitCode') {
        throw 'oracle result metadata is invalid'
    }
    $runResult = $run[0]
    $builds += [pscustomobject]@{
        Label = $label
        EngineSha256 = Get-Sha256 $engine
        EngineBytes = (Get-Item -LiteralPath $engine).Length
        OracleSha256 = Get-Sha256 $oracle
        OracleBytes = (Get-Item -LiteralPath $oracle).Length
        FixtureSha256 = Get-Sha256 $fixture
        FixtureBytes = (Get-Item -LiteralPath $fixture).Length
        OracleExitCode = $runResult.ExitCode
        StdoutBytes = $runResult.StdoutBytes
        StderrBytes = $runResult.StderrBytes
    }
}

$first = $builds[0]
$second = $builds[1]
if ($first.EngineSha256 -ne $second.EngineSha256 -or
    $first.EngineBytes -ne $second.EngineBytes -or
    $first.OracleSha256 -ne $second.OracleSha256 -or
    $first.OracleBytes -ne $second.OracleBytes) {
    throw 'clean builds are not byte-identical'
}
if ($first.FixtureSha256 -ne $ExpectedFixtureSha256 -or
    $second.FixtureSha256 -ne $ExpectedFixtureSha256) {
    throw 'fixture hash does not match the pinned source'
}

[pscustomobject]@{
    Category = 'passed'
    Commit = $PinnedCommit
    SourceDateEpoch = $sourceDateEpoch
    EngineSha256 = $first.EngineSha256
    EngineBytes = $first.EngineBytes
    OracleSha256 = $first.OracleSha256
    OracleBytes = $first.OracleBytes
    FixtureSha256 = $first.FixtureSha256
    FixtureBytes = $first.FixtureBytes
    BuildCount = $builds.Count
    OracleExitCode = $first.OracleExitCode
    StdoutBytes = $first.StdoutBytes
    StderrBytes = $first.StderrBytes
    MaxStreamBytes = $MaxStreamBytes
}
