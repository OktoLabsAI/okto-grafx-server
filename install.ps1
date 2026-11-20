<#
Okto Grafx Server - installer (Windows).

  powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/OktoLabsAI/okto-grafx-server/v0.2.0/install.ps1').Content))"

Or save it and run the file, which is the version you can read before you run:

  curl.exe -fsSL -o install.ps1 https://raw.githubusercontent.com/OktoLabsAI/okto-grafx-server/v0.2.0/install.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Port 8899

What it does, in this order - the same order and the same promises as install.sh, in the Windows
spelling of each step:

  1. checks there is a Python >= 3.11, or a uv that can supply one;
  2. downloads the published wheel for one fixed version and verifies its SHA-256 against the
     release's SHA256SUMS;
  3. installs it into an environment of its own - `uv tool install` when uv is present, otherwise a
     venv under %LOCALAPPDATA%\Programs - and puts `oktografx-server` in <prefix>\bin;
  4. creates the state directory, but only when there is not one already: re-running `init` would
     mint a new administrator token over the old one, and an installer that surprises you about that
     is worse than one that stops;
  5. starts the daemon on 127.0.0.1 as a detached hidden process and waits for /api/v1/health;
  6. tells you what opened. By default that is: no token, and the console answers this machine only.
     With -WithAdminToken it prints the token once, keeps a copy at <state>\admin-token with an
     owner-only ACL, and names the command that mints a new one if that file is ever lost.

Nothing is written outside %LOCALAPPDATA% and %USERPROFILE% unless you pass -Prefix, and -DryRun
prints every command without touching the filesystem - including the uv path, which is the one step
in here that would otherwise do real work.

About the checksum: it comes from the same release as the wheel, so it proves the download was not
truncated or altered in transit, not that the release is authentic. Pin the URL to a tag (as above),
or download the two files and check them yourself before running anything.

This targets Windows PowerShell 5.1 - the one that ships with Windows - so it avoids syntax and
Invoke-WebRequest conveniences that only exist in PowerShell 7: `-UseBasicParsing` everywhere, no
`??`, no ternary, and an explicit TLS floor because 5.1 has no -MinimumProtocolVersion.

Keep this file ASCII and UTF-8-without-BOM: Windows PowerShell 5.1 decodes a BOM-less
script as the machine's ANSI codepage, so a single em dash in a message arrives at the operator as
three characters of mojibake - and no grep in the test harness would ever notice.
#>

[CmdletBinding()]
param(
    # Version to install. The default is the one this script was published with, so a pinned URL stays
    # a pinned version even after the tag moves.
    [string] $Version = '0.2.0',
    # Where the oktografx-server command goes.
    [string] $Prefix,
    # Where server.toml / server.db live. Empty means: ask the same way the server does, which reads
    # OKTO_GRAFX_SERVER_STATE, then XDG_STATE_HOME, then %LOCALAPPDATA%.
    [string] $StateDir,
    # Allowed store root passed to init.
    [string] $Root,
    [ValidatePattern('^\d+$')]
    [string] $Port = '',
    # Interpreter to build the venv with (needs >= 3.11). Unused when uv supplies the interpreter.
    [string] $Python,
    [ValidateSet('auto', 'uv', 'pip')]
    [string] $Installer = 'auto',
    # Install this wheel instead of downloading one - the airgapped route, and the route the release
    # workflow uses to verify its own artifacts.
    [string] $Wheel,
    # Install this source tree instead of a wheel (development, CI).
    [string] $FromTree,
    [switch] $NoInit,
    [switch] $NoRun,
    # Require an administrator token. Without it the console is open to this machine only, which is
    # the default: nothing is minted and nothing is printed.
    [switch] $WithAdminToken,
    # With -WithAdminToken, print the token instead of also writing <state>\admin-token.
    [switch] $NoTokenFile,
    # Append <prefix>\bin to the *user* PATH. Off by default: install.sh does not touch PATH either,
    # and an installer that edits your environment without being asked has to earn it.
    [switch] $AddToPath,
    [switch] $DryRun,
    [switch] $Help
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

# A terminating error inside a script that nobody can debug on the machine it ran on has to say where it
# was. Without this, a parameter binding mistake reaches the operator as
# "ParameterArgumentValidationErrorEmptyStringNotAllowed" with no statement, and the only way to find it
# is to reproduce on a Windows box they do not have.
trap {
    $where = 'an unknown line'
    if ($_.InvocationInfo -and $_.InvocationInfo.ScriptLineNumber -gt 0) {
        $where = "line $($_.InvocationInfo.ScriptLineNumber): $($_.InvocationInfo.Line.Trim())"
    }
    Write-Host ''
    Write-Host "error: $($_.Exception.Message)"
    Write-Host "       at $where"
    Write-Host ''
    exit 1
}

$Repo = 'OktoLabsAI/okto-grafx-server'
$Binary = 'oktografx-server'
$Package = 'okto_grafx_server'

function Show-Usage {
@"
Okto Grafx Server installer (Windows)

  powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing -Uri '<url>/install.ps1').Content))" [options]
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 [options]

options
  -Version V          version to install (default: the one this script was published with)
  -Prefix DIR         where the oktografx-server command goes (default: %LOCALAPPDATA%\Programs\okto-grafx-server)
  -StateDir DIR       where server.toml / server.db live (default: %LOCALAPPDATA%\okto-grafx-server)
  -Root DIR           allowed store root passed to init (default: %USERPROFILE%\graphs)
  -Port PORT          loopback port (default: 8737)
  -Python EXE         interpreter to build the venv with (needs >= 3.11)
  -Installer NAME     auto | uv | pip
  -Wheel FILE         install this wheel instead of downloading one
  -FromTree DIR       install this source tree instead of a wheel (development, CI)
  -NoInit             install the command only, create no state directory
  -NoRun              do not start the daemon
  -WithAdminToken     require an administrator token (default: none is minted, and the console
                      answers this machine only)
  -NoTokenFile        with -WithAdminToken, print the token instead of also writing the file
  -AddToPath          append <prefix>\bin to the user PATH
  -DryRun             print the commands, change nothing
  -Help               this text

environment
  OKG_PREFIX       default for -Prefix
  OKG_GRAPH_ROOT   default for -Root
  OKG_PORT         default for -Port
  OKG_BASE_URL     release directory to download from (https only, e.g. an internal artefact proxy);
                   by default the GitHub release for this version
"@
}

function Log { param([string] $Message) Write-Host ''; Write-Host "== $Message" }
function Say { param([string] $Message) Write-Host "   $Message" }
function Stop-WithError { param([string] $Message) Write-Host ''; Write-Host "error: $Message"; Write-Host ''; exit 1 }

# Native commands write diagnostics to stderr, and PowerShell 5.1 turns a stderr line from a native
# program into an ErrorRecord - which, under "Stop", becomes a NativeCommandError and aborts the script
# on a command that exited 0. Every external program therefore runs through here, with the preference
# relaxed for the duration, and exit code plus merged output handed back together. As an object, not an
# array: PowerShell flattens an array it returns, so @($code, $lines) would arrive as code, first line,
# second line and the caller would be indexing into log output.
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string] $File,
        [string[]] $Arguments = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 1
    $lines = @()
    try {
        $collected = & $File @Arguments 2>&1
        $code = $LASTEXITCODE
        foreach ($line in $collected) { $lines += [string] $line }
    } catch {
        $lines += [string] $_
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ Code = $code; Lines = $lines }
}

function Invoke-NativeLogged {
    param(
        [string] $File,
        [string[]] $Arguments = @(),
        [string] $WhatFailed
    )
    $result = Invoke-Native -File $File -Arguments $Arguments
    if ($result.Code -ne 0) {
        foreach ($line in $result.Lines) { Say $line }
        Stop-WithError "$WhatFailed (exit $($result.Code))"
    }
    return $result
}

function Test-PythonAtLeast {
    # Ask the interpreter for its version and read the number, rather than running a `-c` snippet. A
    # one-liner there means semicolons, parentheses and a `>=` through PowerShell's native argument
    # quoting, and the failure is a SyntaxError from a file that does not exist: python.exe line 1, with
    # the source echoed back and no way to tell what it was actually given. `--version` needs no quotes,
    # and Python 2 prints to stderr, which Invoke-Native has already merged.
    param(
        [string] $File,
        [string[]] $ArgPrefix = @(),
        [int] $Major = 3,
        [int] $Minor = 11
    )
    $result = Invoke-Native $File ($ArgPrefix + @('--version'))
    $text = ($result.Lines -join ' ')
    $match = [regex]::Match($text, 'Python (\d+)\.(\d+)')
    if (-not $match.Success) {
        # A hashtable literal separates entries with semicolons. A comma parses as an array element and
        # the error message points at the next token, which is a confusing way to learn it.
        return [pscustomobject]@{ Ok = $false; Version = ''; Detail = "no version in: $text" }
    }
    $found = "$($match.Groups[1].Value).$($match.Groups[2].Value)"
    $ok = ([int] $match.Groups[1].Value -gt $Major) -or `
        (([int] $match.Groups[1].Value -eq $Major) -and ([int] $match.Groups[2].Value -ge $Minor))
    return [pscustomobject]@{ Ok = $ok; Version = $found; Detail = "Python $found" }
}

function Get-Exe {
    param([string] $Name)
    $found = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $found) { return $null }
    return $found.Source
}

function Get-EnvDefault {
    # $env:SomeName that nobody set is $null under StrictMode 2.0 rather than an error, but a $null
    # left to Join-Path is an exception with no useful message, so defaults resolve through here.
    param([string] $Name, [string] $Fallback)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value }
    return $Fallback
}

function Invoke-Download {
    param(
        [string] $Url,
        [string] $OutFile
    )
    # TLS 1.2 as a floor, matching `curl --proto '=https' --tlsv1.2`. Windows PowerShell takes its
    # protocol from Schannel defaults that still allow older ones on a long-unpatched box, and 5.1 has
    # no -MinimumProtocolVersion, so the floor is set on the service point. Retry by hand for the same
    # reason: -MaximumRetryCount is not in every 5.1 build.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile -TimeoutSec 120 | Out-Null
            return
        } catch {
            $lastError = $_
            Start-Sleep -Seconds (2 * $attempt)
        }
    }
    throw $lastError
}

function Test-Health {
    param([string] $BaseUrl)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/api/v1/health" -TimeoutSec 2
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if ($Help) { Show-Usage; exit 0 }

# --------------------------------------------------------------------------------- the plan
# Defaults resolve in the same order as install.sh: switch, then environment, then platform habit.
if (-not $Prefix) { $Prefix = Get-EnvDefault 'OKG_PREFIX' (Join-Path (Get-EnvDefault 'LOCALAPPDATA' (Join-Path $env:USERPROFILE '.local')) 'Programs\okto-grafx-server') }
if (-not $Root) { $Root = Get-EnvDefault 'OKG_GRAPH_ROOT' (Join-Path $env:USERPROFILE 'graphs') }
if (-not $Port) { $Port = Get-EnvDefault 'OKG_PORT' '8737' }
if (-not $StateDir) {
    if ([Environment]::GetEnvironmentVariable('OKTO_GRAFX_SERVER_STATE')) {
        $StateDir = [Environment]::GetEnvironmentVariable('OKTO_GRAFX_SERVER_STATE')
    } elseif ([Environment]::GetEnvironmentVariable('XDG_STATE_HOME')) {
        # Honoured here too, because default_state_dir() honours it on every platform: an operator who
        # pins the directory expects the installer and the server to agree about it wherever they run.
        $StateDir = Join-Path ([Environment]::GetEnvironmentVariable('XDG_STATE_HOME')) 'okto-grafx-server'
    } else {
        $StateDir = Join-Path (Get-EnvDefault 'LOCALAPPDATA' (Join-Path $env:USERPROFILE '.local\state')) 'okto-grafx-server'
    }
}
# The environment lives apart from the state on purpose: -PurgeState deletes the state directory, and a
# venv inside it would be deleted along with registrations that point at stores living elsewhere.
$DataDir = Get-EnvDefault 'OKG_DATA_DIR' $Prefix
$BinExe = Join-Path $Prefix 'bin\oktografx-server.exe'
$BinCmd = Join-Path $Prefix 'bin\oktografx-server.cmd'
# One name for the pid file, because three separate places need it: the check below that refuses to
# reinstall over a live server, the write that records the daemon, and the summary line that tells the
# operator how to stop it. Building the path inline each time is how one of them ends up different.
$PidPath = Join-Path $StateDir 'daemon.pid'
$BaseUrl = Get-EnvDefault 'OKG_BASE_URL' "https://github.com/$Repo/releases/download/v$Version"

Log 'Okto Grafx Server'
Say "command       $BinExe"
Say "state         $StateDir"
Say "store root    $Root"
Say "console       http://127.0.0.1:$Port/"
if ($DryRun) { Say 'dry run       nothing is executed or created' }

# --------------------------------------------------------------------------------- interpreter
$UsingUv = $false
$PythonBin = $null
$PythonArgs = @()
if ($Installer -eq 'pip') {
    # Asked for the venv path explicitly, so uv is not consulted even when it is installed.
} elseif ($Installer -eq 'uv') {
    if (-not (Get-Exe 'uv')) { Stop-WithError '-Installer uv, but uv is not on PATH' }
    $UsingUv = $true
} elseif (Get-Exe 'uv') {
    $UsingUv = $true
}

if ($UsingUv) {
    $uvVersion = (Invoke-Native 'uv' @('--version')).Lines -join ' '
    Say "installer     uv ($uvVersion) - it supplies a Python >= 3.11 of its own"
} else {
    if ($Python) {
        $candidates = @($Python)
    } else {
        # `py` first, because the launcher finds every installed CPython. `python3` is in the list but
        # not trusted: on a stock Windows 10/11 it can be the Store execution alias, which is why a
        # candidate whose path mentions WindowsApps is dropped below rather than believed.
        $candidates = @('py', 'python3.13', 'python3.12', 'python3.11', 'python3', 'python')
    }
    $tried = @()
    foreach ($candidate in $candidates) {
        # Resolve through PATH first even when -Python named the interpreter itself: "-Python python" is
        # what a person types, and on Windows that name may be the Store execution alias rather than an
        # interpreter. A path that is not on PATH still works, because Get-Command takes a full path and
        # Test-Path catches the ones it does not.
        $resolved = Get-Exe $candidate
        if (-not $resolved -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { $resolved = $candidate }
        if (-not $resolved) { $tried += "$candidate (not found)"; continue }
        if ($resolved -match 'WindowsApps') {
            $tried += "$candidate (the Microsoft Store alias at $resolved, not an interpreter)"
            continue
        }
        # The launcher needs `-3` before `-c` or it may pick a 2.x; every other interpreter takes the
        # arguments directly, and that prefix has to travel with the path all the way to venv creation.
        # $launcherArgs, never $prefix: PowerShell names are case-insensitive, so a lowercase `$prefix`
        # in this loop *is* the script's `$Prefix` - the install directory - and the wipe shows up two
        # screens later as New-Item refusing an empty path, with no hint of where it was emptied. That is
        # how this run actually failed on the first try on Windows.
        $launcherArgs = @()
        if ((Split-Path -Leaf $resolved) -ieq 'py.exe') { $launcherArgs = @('-3') }
        $probe = Test-PythonAtLeast -File $resolved -ArgPrefix $launcherArgs
        if ($probe.Ok) { $PythonBin = $resolved; $PythonArgs = $launcherArgs; break }
        $tried += "$resolved ($($probe.Detail))"
    }
    if (-not $PythonBin) {
        foreach ($attempt in $tried) { Say "tried         $attempt" }
        Stop-WithError 'no Python >= 3.11 found. Install one (https://www.python.org/downloads/windows/), or install uv (https://docs.astral.sh/uv/) and run this again.'
    }
    $pyVersion = (Invoke-Native $PythonBin ($PythonArgs + @('--version'))).Lines -join ' '
    if (-not $pyVersion) { $pyVersion = "python at $PythonBin" }
    Say "installer     venv + pip ($pyVersion)"
}

# --------------------------------------------------------------------------------- artifact
# mktemp equivalent. Under -DryRun nothing is created, so the directory must not exist either - the
# harness asserts its parent stays empty.
$Work = $null
if (-not $DryRun) {
    $Work = Join-Path (Get-EnvDefault 'TEMP' $env:USERPROFILE) ('okg-install.' + [Guid]::NewGuid().ToString('N').Substring(0, 12))
    New-Item -ItemType Directory -Path $Work -Force | Out-Null
}

function Remove-Work {
    if ($Work -and (Test-Path -LiteralPath $Work)) {
        Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

try {
    $Target = $null
    if ($FromTree) {
        if (-not (Test-Path -LiteralPath $FromTree -PathType Container)) { Stop-WithError "-FromTree is not a directory: $FromTree" }
        $Target = $FromTree
        Say "source        working tree $FromTree"
    } elseif ($Wheel) {
        if (-not (Test-Path -LiteralPath $Wheel -PathType Leaf)) { Stop-WithError "-Wheel is not a file: $Wheel" }
        $Target = $Wheel
        Say "source        $Wheel"
    } else {
        $WheelName = "$Package-$Version-py3-none-any.whl"
        $Target = Join-Path $Work $WheelName
        Log "Downloading $WheelName"
        Say "from          $BaseUrl"
        try {
            Invoke-Download -Url "$BaseUrl/$WheelName" -OutFile $Target
        } catch {
            Stop-WithError "could not download $BaseUrl/$WheelName - is v$Version published? See https://github.com/$Repo/releases, or pass -Version / -Wheel."
        }
        $SumsPath = Join-Path $Work 'SHA256SUMS'
        try {
            Invoke-Download -Url "$BaseUrl/SHA256SUMS" -OutFile $SumsPath
        } catch {
            Stop-WithError "could not download $BaseUrl/SHA256SUMS - refusing to install a wheel it cannot verify."
        }
        # The manifest lists the sdist too, which we never fetch, so it is read by hand instead of being
        # verified in full: a line for a file we do not want must not refuse a perfectly good wheel.
        $expected = $null
        foreach ($line in Get-Content -LiteralPath $SumsPath) {
            $fields = @(($line -split '\s+') | Where-Object { $_ })
            if ($fields.Count -lt 2) { continue }
            $listedName = (($fields[1] -replace '^\*', '') -replace '.*[\\/]', '')
            if ($listedName -eq $WheelName) { $expected = $fields[0].ToLowerInvariant(); break }
        }
        if (-not $expected) {
            Stop-WithError "the release's SHA256SUMS has no line for $WheelName - refusing to install it unverified"
        }
        $actual = (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expected -ne $actual) {
            Write-Host ''
            Write-Host "error: SHA-256 mismatch for $WheelName"
            Write-Host "         expected $expected"
            Write-Host "         got      $actual"
            Write-Host '         Refusing to install it. If the release was republished, re-check the URL you pinned.'
            Write-Host ''
            exit 1
        }
        Say "checksum      $actual (matches SHA256SUMS)"
    }

    # --------------------------------------------------------------------------------- install
    # Windows cannot replace an executable a process has open. Rerunning this script against a running
    # server is routine on Unix - the old binary is unlinked, the daemon carries on from the inode it was
    # started from, pip never notices - and here it is a WinError 32 from the middle of `pip install`,
    # with the environment half-written and nothing in the error to say that stopping the server was the
    # fix. So the script notices first, before it touches anything, and names the command that clears it.
    # A pid file whose process is gone (crash, reboot, recycled pid) is not a running server: only a live
    # process that is actually ours stops the install.
    if (-not $DryRun -and (Test-Path -LiteralPath $PidPath)) {
        $recorded = (Get-Content -LiteralPath $PidPath -Raw).Trim()
        $serving = $null
        try { $serving = Get-Process -Id ([int] $recorded) -ErrorAction Stop } catch { $serving = $null }
        if ($serving -and $serving.ProcessName -like 'oktografx-server*') {
            Stop-WithError "The server is running from this state directory (pid $recorded). Windows cannot replace the executable that process started from, so the package step would fail partway through and leave $DataDir half-installed. Stop it first: Stop-Process -Id $recorded"
        }
    }

    if (-not $DryRun) {
        New-Item -ItemType Directory -Path (Join-Path $Prefix 'bin') -Force | Out-Null
    }

    if ($UsingUv) {
        Log 'Installing with uv'
        # uv keeps its own tool bin; the environment variable is the portable way to point it at
        # -Prefix (`uv tool install` has no --bin-dir), and uv honours it on Windows too.
        $uvArgs = @('tool', 'install', '--reinstall')
        if ($FromTree) { $uvArgs += @('--editable', $FromTree) } else { $uvArgs += $Target }
        if ($DryRun) {
            Say "[dry-run] UV_TOOL_BIN_DIR=$(Join-Path $Prefix 'bin') uv $($uvArgs -join ' ')"
        } else {
            $env:UV_TOOL_BIN_DIR = Join-Path $Prefix 'bin'
            Invoke-NativeLogged -File 'uv' -Arguments $uvArgs -WhatFailed 'uv tool install failed' | Out-Null
            New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $DataDir 'install-method') -Value 'uv' -Encoding ascii
        }
    } else {
        Log "Installing into $DataDir\venv"
        if ($DryRun) {
            Say "[dry-run] $PythonBin $((($PythonArgs + @('-m', 'venv', "$DataDir\venv")) -join ' '))"
            Say "[dry-run] pip install --upgrade pip setuptools wheel"
            Say "[dry-run] pip install $Target"
            Say "[dry-run] write $BinCmd -> $DataDir\venv\Scripts\oktografx-server.exe"
        } else {
            New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
            $venvPython = Join-Path $DataDir 'venv\Scripts\python.exe'
            if (-not (Test-Path -LiteralPath $venvPython)) {
                Invoke-NativeLogged -File $PythonBin -Arguments ($PythonArgs + @('-m', 'venv', (Join-Path $DataDir 'venv'))) -WhatFailed 'could not create the venv' | Out-Null
            }
            Invoke-NativeLogged -File $venvPython -Arguments @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip', 'setuptools', 'wheel') -WhatFailed 'pip could not upgrade itself' | Out-Null
            Invoke-NativeLogged -File $venvPython -Arguments @('-m', 'pip', 'install', '--quiet', "$Target") -WhatFailed 'pip install failed' | Out-Null
            # The POSIX installer symlinks <prefix>/bin/oktografx-server into the venv. A symlink needs
            # developer mode or elevation on Windows, so the same effect is a two-line shim: the thing on
            # PATH is ours, and the uninstaller can tell that by reading it.
            $shim = "@echo off`r`n`"%~dp0..\venv\Scripts\oktografx-server.exe`" %*"
            Set-Content -LiteralPath $BinCmd -Value $shim -Encoding ascii
            Set-Content -LiteralPath (Join-Path $DataDir 'install-method') -Value 'venv' -Encoding ascii
        }
    }

    # Which of the two spellings of the command actually exists, and whether it runs.
    $Bin = $null
    $DaemonBin = $null
    if (-not $DryRun) {
        if (Test-Path -LiteralPath $BinExe -PathType Leaf) { $Bin = $BinExe }
        elseif (Test-Path -LiteralPath $BinCmd -PathType Leaf) { $Bin = $BinCmd }
        else { Stop-WithError "installed, but neither $BinExe nor $BinCmd exists" }
        # What `Start-Process` gets: the venv path puts a two-line .cmd shim in bin because a symlink
        # needs developer mode, and Start-Process cannot redirect the output of a batch file without
        # ShellExecute - and with ShellExecute the pid it hands back is the shell's, not the daemon's.
        # The venv's own console-script executable is a real process with a real pid, so the pid file
        # names the thing that is actually serving the port.
        $DaemonBin = $Bin
        if (($Bin -ieq $BinCmd) -and (Test-Path -LiteralPath (Join-Path $DataDir 'venv\Scripts\oktografx-server.exe') -PathType Leaf)) {
            $DaemonBin = Join-Path $DataDir 'venv\Scripts\oktografx-server.exe'
        }
        $reported = Invoke-Native $Bin @('--version')
        if ($reported.Code -ne 0) { Stop-WithError "installed, but `"$Bin --version`" exited $($reported.Code): $($reported.Lines -join ' ')" }
        Say "command       $($reported.Lines | Select-Object -First 1)"
        Say "              $Bin"
        $binDir = Join-Path $Prefix 'bin'
        $onPath = $false
        foreach ($entry in ($env:PATH -split ';')) {
            if ($entry -and ($entry.TrimEnd('\') -ieq $binDir)) { $onPath = $true; break }
        }
        if ($AddToPath) {
            $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
            if (-not $userPath) { $userPath = '' }
            if (@($userPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $binDir }).Count -eq 0) {
                $joined = if ($userPath) { "$userPath;$binDir" } else { $binDir }
                [Environment]::SetEnvironmentVariable('Path', $joined, 'User')
                $env:PATH = "$env:PATH;$binDir"
                Say "path          added $binDir to your user PATH (open a new terminal for it to apply)"
            } else {
                Say 'path          already on PATH'
            }
        } elseif (-not $onPath) {
            Say "note          $binDir is not on PATH - add it, re-run with -AddToPath, or call the full path"
        }
    }

    # --------------------------------------------------------------------------------- state
    $Token = ''
    $TokenPath = Join-Path $StateDir 'admin-token'
    if (-not $NoInit) {
        $ServerToml = Join-Path $StateDir 'server.toml'
        if (Test-Path -LiteralPath $ServerToml -PathType Leaf) {
            Log 'State directory already exists'
            Say "leaving $StateDir untouched"
            Say "'init' a second time would mint a new administrator token over yours;"
            Say "require one deliberately with: oktografx-server --state-dir $StateDir admin-token rotate"
        } else {
            Log 'Creating the state directory'
            if ($DryRun) {
                Say "[dry-run] mkdir $Root"
                $flag = if ($WithAdminToken) { ' --with-admin-token' } else { '' }
                Say "[dry-run] oktografx-server --state-dir $StateDir init --root $Root --port $Port$flag"
            } else {
                # `init` records allowed_roots; a root that does not exist yet would make every later
                # `db add` under it a refusal, so the installer creates the one it just promised.
                if (-not (Test-Path -LiteralPath $Root)) { New-Item -ItemType Directory -Path $Root -Force | Out-Null }
                # And it records the resolved path, because the daemon refuses a store path with a
                # reparse point in it - on Windows that is a junction, and a root stored through one
                # would reject every store underneath it with a message about symlinks that points
                # nowhere near the installer.
                $ResolvedRoot = (Resolve-Path -LiteralPath $Root).ProviderPath.TrimEnd('\')
                if ($ResolvedRoot -ine $Root.TrimEnd('\')) { Say "store root    $ResolvedRoot (links resolved from $Root)" }
                $initArgs = @('--state-dir', $StateDir, 'init', '--root', $ResolvedRoot, '--port', $Port)
                if ($WithAdminToken) { $initArgs += '--with-admin-token' }
                $init = Invoke-Native $Bin $initArgs
                if ($init.Code -ne 0) {
                    foreach ($line in $init.Lines) { Say $line }
                    Stop-WithError "init refused to create $StateDir"
                }
                if ($WithAdminToken) {
                    $Token = ''
                    foreach ($line in $init.Lines) {
                        $match = [regex]::Match($line, 'okga_[A-Za-z0-9_-]{16,}')
                        if ($match.Success) { $Token = $match.Value; break }
                    }
                    if (-not $Token) {
                        foreach ($line in $init.Lines) { Say $line }
                        Stop-WithError 'init succeeded but printed no administrator token'
                    }
                    if (-not $NoTokenFile) {
                        Set-Content -LiteralPath $TokenPath -Value $Token -Encoding ascii
                        # The POSIX installer creates this file under umask 177. NTFS has no umask, so the
                        # equivalent of "owner only" is an access list holding one entry, for this account,
                        # with nothing inherited from the folder above. That is built here through the ACL
                        # object rather than icacls: the command line answers in prose, and "1 file
                        # processed" says nothing about which entries the file ends up carrying - while
                        # this script's promise is about exactly those entries. So they are set, read back,
                        # and checked. If it fails the file still sits in a profile folder this account
                        # owns, but the promise would be false, so the script says so rather than being
                        # quiet about it.
                        $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
                        $locked = $false
                        try {
                            $acl = Get-Acl -LiteralPath $TokenPath
                            # Protected, and not preserving what came from the parent: a token file must
                            # not become readable next month because somebody relaxed its folder.
                            $acl.SetAccessRuleProtection($true, $false)
                            # Whatever the profile folder granted - Users, Authenticated Users, an
                            # Administrators group this account happens to be in - comes out, because the
                            # claim being made is "this account alone", not "this account plus whoever the
                            # parent felt like including".
                            foreach ($explicit in @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))) {
                                if ($explicit.IdentityReference.Value -ne $sid.Value) {
                                    $acl.PurgeAccessRules($explicit.IdentityReference)
                                }
                            }
                            $acl.SetAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
                                $sid,
                                [System.Security.AccessControl.FileSystemRights]::FullControl,
                                [System.Security.AccessControl.InheritanceFlags]::None,
                                [System.Security.AccessControl.PropagationFlags]::None,
                                [System.Security.AccessControl.AccessControlType]::Allow))
                            Set-Acl -LiteralPath $TokenPath -AclObject $acl
                            $written = Get-Acl -LiteralPath $TokenPath
                            $strangers = @($written.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
                                Where-Object { ($_.AccessControlType -eq 'Allow') -and ($_.IdentityReference.Value -ne $sid.Value) })
                            $locked = ($written.AreAccessRulesProtected -and $strangers.Count -eq 0)
                        } catch {
                            $locked = $false
                        }
                        if ($locked) {
                            Say "token file    $TokenPath (one entry: this account. nothing inherited)"
                        } else {
                            Say "token file    $TokenPath - WARNING: the access list could not be set to this account alone; the file keeps the folder's inherited permissions"
                        }
                    }
                } else {
                    Say 'no administrator token minted: the console will answer this machine only'
                }
                Say 'server.toml and server.db written'
            }
        }
    }

    # --------------------------------------------------------------------------------- start
    $DaemonRunning = $false
    $HealthUrl = "http://127.0.0.1:$Port"
    if (-not $NoRun) {
        if (Test-Health $HealthUrl) {
            Log "Something already answers on 127.0.0.1:$Port"
            $DaemonRunning = $true
            Say 'not starting a second daemon; stop the existing one first if you meant to replace it'
        } elseif ($DryRun) {
            Say "[dry-run] start /min `"$Bin`" --state-dir `"$StateDir`" run --port $Port"
        } else {
            Log 'Starting the daemon'
            # Start-Process detaches: the daemon outlives this shell the way `nohup ... &` does. There
            # is no supervisor behind it - nothing relaunches it after a crash, nothing brings it back
            # after a reboot - and the summary says so rather than implying otherwise.
            # -NoNewWindow rather than -WindowStyle Hidden: 5.1 cannot honour Hidden and the redirections
            # together, and asking for Hidden left the daemon holding the installer's console - including
            # the write end of the caller's stdout pipe. The installer exited, the server kept serving,
            # and whoever was reading that pipe waited for an EOF a live daemon never sends: the job hung
            # until the runner killed an orphan oktografx-server 25 minutes later. Requesting the
            # redirections forces a windowless start by itself, so nothing flashes on a desktop either.
            # Start-Process joins an -ArgumentList array with spaces and does not quote for you, so the
            # arguments are quoted here: %USERPROFILE% is routinely `C:\Users\First Last`.
            $runArgs = "--state-dir `"$StateDir`" run --port $Port"
            $stdout = Join-Path $StateDir 'server.stdout'
            $stderr = Join-Path $StateDir 'server.stderr'
            $started = Start-Process -FilePath $DaemonBin -ArgumentList $runArgs -NoNewWindow `
                -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
            Set-Content -LiteralPath $PidPath -Value $started.Id -Encoding ascii
            for ($attempt = 0; $attempt -lt 60; $attempt++) {
                if (Test-Health $HealthUrl) { $DaemonRunning = $true; break }
                Start-Sleep -Milliseconds 500
            }
            if (-not $DaemonRunning) {
                Stop-WithError "the daemon did not answer on 127.0.0.1:$Port - see $stdout and $stderr"
            }
            Say "pid $($started.Id) - /api/v1/health is answering"
        }
    }

    # --------------------------------------------------------------------------------- handover
    Log 'Ready'
    Say "console       http://127.0.0.1:$Port/"
    $TokenShown = $false
    if ($Token) {
        Say "token         $Token"
        Say "              minted now, printed once by the server; the copy is $TokenPath"
        $TokenShown = $true
    } elseif ((-not $DryRun) -and (Test-Path -LiteralPath $TokenPath -PathType Leaf)) {
        # Showing it again is the point. An installer that says "it was printed once, sometime, in an
        # earlier run" leaves the operator to guess where the value went, and the value is already in a
        # file this user owns and can read.
        Say "token         $((Get-Content -LiteralPath $TokenPath -Raw).Trim())"
        Say "              minted by an earlier run; this is the copy in $TokenPath"
        $TokenShown = $true
    } else {
        # Nothing to show. Whether that means "no token exists" or "one exists and you have lost it" is
        # the server's answer, not ours to guess: the control database knows, a terminal scrollback does
        # not.
        $TokenRequired = $false
        if ((-not $DryRun) -and $Bin) {
            $status = Invoke-Native $Bin @('--state-dir', $StateDir, 'admin-token', 'status')
            if (($status.Lines -join ' ') -match 'a token is required') { $TokenRequired = $true }
        }
        if ($TokenRequired) {
            Say "token         required, and not in $TokenPath. The server keeps only a hash, so nothing"
            Say '              can be recovered from it: mint one you can use'
            Say "              oktografx-server --state-dir $StateDir admin-token rotate"
        } else {
            Say 'access        no token required. The console answers this machine only - any process'
            Say '              that can reach 127.0.0.1 is administrator here, anything from elsewhere is'
            Say '              refused'
            Say "require one   oktografx-server --state-dir $StateDir admin-token rotate"
            Say '              or the same switch in Settings, which shows the value once'
        }
    }
    if ($TokenShown) {
        Say "login         open http://127.0.0.1:$Port/ and paste it into the sign-in screen"
        Say "lost it?      oktografx-server --state-dir $StateDir admin-token rotate"
        Say '              mints a new token and invalidates the old one, and every session signed in with it'
    }
    Say 'attach        oktografx-server db add C:\path\to\store.grafx --slug mystore --read-only'
    Say 'stores        oktografx-server status'
    Say "stop          Stop-Process -Id (Get-Content $PidPath)"
    Say "logs          $(Join-Path $StateDir 'server.log'), plus server.stdout and server.stderr"
    Say "uninstall     curl.exe -fsSL -o uninstall.ps1 https://raw.githubusercontent.com/$Repo/v$Version/uninstall.ps1"
    Say '              powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1'
    Write-Host ''
    Write-Host '   The store stays embedded and on local disk. This daemon holds it open for you; a local'
    Write-Host '   process with filesystem access can open the same store directly, so what sits here is'
    Write-Host '   logical access control, not an operating-system sandbox. Read-only until you say'
    Write-Host '   otherwise. The daemon is a plain hidden process, not a service: it will not restart'
    Write-Host '   itself after a crash and it will not come back after a reboot.'
    Write-Host ''
    exit 0
} finally {
    Remove-Work
}
