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
$MaxBuildStreamBytes = 65536
$TimeoutMilliseconds = 30000
$ExpectedOlmoeBasename = 'olmoe.exe'
$BuildTimeoutMilliseconds = 900000
$BuildTerminationWaitMilliseconds = 5000

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

function Test-NoEmbeddedBuildRoot {
    # Deterministic (SOURCE_DATE_EPOCH-pinned) builds must not bake the
    # absolute clean-build directory into the produced binary; otherwise two
    # builds performed under different roots could never be byte-identical
    # for a reason unrelated to actual reproducibility.
    param([string]$BinaryPath, [string]$ForbiddenRoot)

    $bytes = [IO.File]::ReadAllBytes($BinaryPath)
    $needleAnsi = [Text.Encoding]::ASCII.GetBytes($ForbiddenRoot)
    $needleUtf8 = [Text.Encoding]::UTF8.GetBytes($ForbiddenRoot)
    foreach ($needle in @($needleAnsi, $needleUtf8)) {
        if ($needle.Length -eq 0) {
            continue
        }
        $limit = $bytes.Length - $needle.Length
        for ($offset = 0; $offset -le $limit; $offset++) {
            $matched = $true
            for ($index = 0; $index -lt $needle.Length; $index++) {
                if ($bytes[$offset + $index] -ne $needle[$index]) {
                    $matched = $false
                    break
                }
            }
            if ($matched) {
                return $false
            }
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

function Get-DescendantProcessIds {
    # A breadth-first walk of the live process table rooted at
    # $ProcessId, used only to *prove* (after a kill attempt) that no
    # compiler descendant is knowingly left running -- never to perform
    # the termination itself.
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $descendants = [Collections.Generic.List[int]]::new()
    $frontier = [Collections.Generic.Queue[int]]::new()
    $frontier.Enqueue($ProcessId)
    while ($frontier.Count -gt 0) {
        $current = $frontier.Dequeue()
        $children = Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId=$current" -ErrorAction SilentlyContinue
        foreach ($child in $children) {
            $childId = [int]$child.ProcessId
            if (Get-Process -Id $childId -ErrorAction SilentlyContinue) {
                $descendants.Add($childId)
                $frontier.Enqueue($childId)
            }
        }
    }
    return $descendants
}

function Stop-ProcessTree {
    # Kills the complete process tree rooted at $ProcessId. Prefers
    # Process.Kill(true) (recursive tree kill), available where the
    # hosting PowerShell runtime targets .NET Core 3+; falls back to
    # `taskkill /PID /T /F`, which performs the same recursive tree kill
    # on every supported Windows PowerShell edition.
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $target = $null
    try {
        $target = Get-Process -Id $ProcessId -ErrorAction Stop
    } catch {
        return
    }
    $killWithTree = $target.GetType().GetMethod('Kill', [Type[]]@([bool]))
    if ($null -ne $killWithTree) {
        try {
            $killWithTree.Invoke($target, @($true)) | Out-Null
            return
        } catch {
            # Fall through to the taskkill fallback below.
        }
    }
    & taskkill.exe /PID $ProcessId /T /F *> $null
}

function Invoke-BoundedBuild {
    # Runs one MSYS2 UCRT64 make invocation with bounded, redirected
    # output under a fixed absolute time limit. SOURCE_DATE_EPOCH is
    # supplied only to this child process's own environment block --
    # ProcessStartInfo.EnvironmentVariables starts as a private copy of
    # the current process environment, so setting a key on it changes
    # only this child's block and never mutates or deletes the caller's
    # actual (parent PowerShell) environment.
    param(
        [Parameter(Mandatory = $true)][string]$Launcher,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string]$SourceDateEpoch
    )

    $stdoutFile = Join-Path $WorkingDirectory 'build.stdout.bin'
    $stderrFile = Join-Path $WorkingDirectory 'build.stderr.bin'

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Launcher
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.Arguments = "-defterm -here -no-start -ucrt64 -c `"$Command`""
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables['SOURCE_DATE_EPOCH'] = $SourceDateEpoch

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw 'native build could not be started'
    }
    $processId = $process.Id

    $stdoutStream = [IO.File]::Create($stdoutFile)
    $stderrStream = [IO.File]::Create($stderrFile)
    $stdoutCopy = $process.StandardOutput.BaseStream.CopyToAsync($stdoutStream)
    $stderrCopy = $process.StandardError.BaseStream.CopyToAsync($stderrStream)
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $failureReason = $null
    try {
        while (-not $process.WaitForExit(10)) {
            $stdoutStream.Flush()
            $stderrStream.Flush()
            if ($stdoutStream.Length -gt $MaxBuildStreamBytes -or
                $stderrStream.Length -gt $MaxBuildStreamBytes) {
                $failureReason = 'native build output exceeded the fixed limit'
                break
            }
            if ($timer.ElapsedMilliseconds -ge $BuildTimeoutMilliseconds) {
                $failureReason = 'native build exceeded the fixed time limit'
                break
            }
        }

        if ($null -ne $failureReason) {
            # Bounded or overflowing output means raw build output is
            # never allowed to reach the caller -- the process tree is
            # terminated below and only a fixed, path-free error is ever
            # raised, never the captured bytes themselves.
            Stop-ProcessTree -ProcessId $processId
            if (-not $process.WaitForExit($BuildTerminationWaitMilliseconds)) {
                throw 'native build launcher did not exit after termination'
            }
            if ((Get-DescendantProcessIds -ProcessId $processId).Count -gt 0) {
                throw 'native build left a compiler descendant running'
            }
            throw $failureReason
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
    $timer.Stop()

    $stdoutBytes = (Get-Item -LiteralPath $stdoutFile).Length
    $stderrBytes = (Get-Item -LiteralPath $stderrFile).Length
    if ($stdoutBytes -gt $MaxBuildStreamBytes -or $stderrBytes -gt $MaxBuildStreamBytes) {
        throw 'native build output exceeded the fixed limit'
    }
    if ($exitCode -ne 0) {
        throw 'native build failed'
    }

    return [pscustomobject]@{
        ElapsedMs = $timer.ElapsedMilliseconds
        StdoutBytes = $stdoutBytes
        StderrBytes = $stderrBytes
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
    $buildResult = Invoke-BoundedBuild -Launcher $launcher -WorkingDirectory $cRoot -SourceDateEpoch $sourceDateEpoch -Command (
        'make glm.exe ARCH=x86-64-v3 && ' +
        'make tests/test_idot.exe ARCH=x86-64-v3 && ' +
        'make olmoe.exe ARCH=x86-64-v3'
    )

    $engine = Join-Path $cRoot 'glm.exe'
    $oracle = Join-Path $cRoot 'tests\test_idot.exe'
    $fixture = Join-Path $cRoot 'tests\test_idot.c'
    $olmoe = Join-Path $cRoot $ExpectedOlmoeBasename
    if (-not (Test-Path -LiteralPath $olmoe -PathType Leaf) -or
        (Get-Item -LiteralPath $olmoe).Name -cne $ExpectedOlmoeBasename) {
        throw 'olmoe.exe was not produced with the exact expected basename'
    }
    if (-not (Test-NoEmbeddedBuildRoot -BinaryPath $olmoe -ForbiddenRoot $target)) {
        throw 'olmoe.exe embeds the clean-build root path'
    }
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
        OlmoeSha256 = Get-Sha256 $olmoe
        OlmoeBytes = (Get-Item -LiteralPath $olmoe).Length
        OlmoeBuildDurationMs = $buildResult.ElapsedMs
        OlmoeBuildStdoutBytes = $buildResult.StdoutBytes
        OlmoeBuildStderrBytes = $buildResult.StderrBytes
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
$olmoeDeterministicallyEqual = (
    $first.OlmoeSha256 -eq $second.OlmoeSha256 -and $first.OlmoeBytes -eq $second.OlmoeBytes
)
if (-not $olmoeDeterministicallyEqual) {
    throw 'olmoe.exe clean builds are not byte-identical'
}

$result = [pscustomobject]@{
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
    Olmoe = [pscustomobject]@{
        Category = 'passed'
        Commit = $PinnedCommit
        OlmoeBasename = $ExpectedOlmoeBasename
        OlmoeBytes = $first.OlmoeBytes
        OlmoeSha256A = $first.OlmoeSha256
        OlmoeSha256B = $second.OlmoeSha256
        DeterministicallyEqual = $olmoeDeterministicallyEqual
        BuildDurationMsA = $first.OlmoeBuildDurationMs
        BuildDurationMsB = $second.OlmoeBuildDurationMs
        BuildStdoutBytesA = $first.OlmoeBuildStdoutBytes
        BuildStdoutBytesB = $second.OlmoeBuildStdoutBytes
        BuildStderrBytesA = $first.OlmoeBuildStderrBytes
        BuildStderrBytesB = $second.OlmoeBuildStderrBytes
        MaxBuildStreamBytes = $MaxBuildStreamBytes
    }
}

$result | ConvertTo-Json -Depth 8
