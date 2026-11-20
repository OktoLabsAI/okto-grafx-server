<#
Okto Grafx Server - uninstaller (Windows).

  curl.exe -fsSL -o uninstall.ps1 https://raw.githubusercontent.com/OktoLabsAI/okto-grafx-server/v0.2.0/uninstall.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1

Removes what install.ps1 put there: the `oktografx-server` command and the environment it lives in.

It deliberately does not touch the state directory (server.toml, server.db, the administrator token,
the audit trail) or any Grafx store. A registration is a reference to a store that lives wherever you
put it, and an uninstall should not quietly rewrite facts about your data. -PurgeState removes the
control plane on purpose - and still never deletes a store.
#>

[CmdletBinding()]
param(
    # Where the oktografx-server command lives, and where its venv sits next to it.
    [string] $Prefix,
    # The state directory, if it is not the default.
    [string] $StateDir,
    # Also remove the state directory: registrations, keys, audit trail, token.
    [switch] $PurgeState,
    # Also take <prefix>\bin out of the user PATH. Without it the uninstaller prints the line that does
    # it, because removing something from your PATH that you might have edited yourself is not a call
    # this script gets to make quietly.
    [switch] $RemoveFromPath,
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

function Show-Usage {
@"
Okto Grafx Server uninstaller (Windows)

options
  -Prefix DIR        where the oktografx-server command lives (default: %LOCALAPPDATA%\Programs\okto-grafx-server)
  -StateDir DIR      the state directory, if it is not the default
  -PurgeState        also remove the state directory: registrations, keys, audit trail, token
  -RemoveFromPath    take <prefix>\bin out of the user PATH
  -DryRun            print what would be removed, remove nothing
  -Help              this text
"@
}

function Log { param([string] $Message) Write-Host ''; Write-Host "== $Message" }
function Say { param([string] $Message) Write-Host "   $Message" }
function Stop-WithError { param([string] $Message) Write-Host ''; Write-Host "error: $Message"; Write-Host ''; exit 1 }

function Get-EnvDefault {
    param([string] $Name, [string] $Fallback)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value }
    return $Fallback
}

# Same flattening trap as install.ps1: exit code and merged output come back as an object, and stderr
# from a native command must not be allowed to become a NativeCommandError under "Stop".
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

function Remove-Path2 {
    # `Remove-Item -LiteralPath` plus the dry-run promise: the harness asserts the tree is untouched
    # afterwards, so every deletion in here goes through this one function.
    param([string] $Target)
    if ($DryRun) {
        Say "[dry-run] remove $Target"
        return
    }
    Remove-Item -LiteralPath $Target -Recurse -Force -ErrorAction SilentlyContinue
}

if ($Help) { Show-Usage; exit 0 }

if (-not $Prefix) { $Prefix = Get-EnvDefault 'OKG_PREFIX' (Join-Path (Get-EnvDefault 'LOCALAPPDATA' (Join-Path $env:USERPROFILE '.local')) 'Programs\okto-grafx-server') }
if (-not $StateDir) {
    if ([Environment]::GetEnvironmentVariable('OKTO_GRAFX_SERVER_STATE')) {
        $StateDir = [Environment]::GetEnvironmentVariable('OKTO_GRAFX_SERVER_STATE')
    } elseif ([Environment]::GetEnvironmentVariable('XDG_STATE_HOME')) {
        $StateDir = Join-Path ([Environment]::GetEnvironmentVariable('XDG_STATE_HOME')) 'okto-grafx-server'
    } else {
        $StateDir = Join-Path (Get-EnvDefault 'LOCALAPPDATA' (Join-Path $env:USERPROFILE '.local\state')) 'okto-grafx-server'
    }
}

$BinDir = Join-Path $Prefix 'bin'
$BinExe = Join-Path $BinDir "$Binary.exe"
$BinCmd = Join-Path $BinDir "$Binary.cmd"
$MethodFile = Join-Path $Prefix 'install-method'
$Method = 'venv'
if (Test-Path -LiteralPath $MethodFile -PathType Leaf) { $Method = (Get-Content -LiteralPath $MethodFile -Raw).Trim() }

Log 'Removing the command'
$removedAny = $false
foreach ($candidate in @($BinExe, $BinCmd)) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    # What install.ps1 leaves here is either its own two-line shim into our venv, or uv's entry point in
    # the bin directory we pointed it at. Anything else living at this path belongs to somebody else and
    # stays - the same rule uninstall.sh enforces by reading a symlink target or a shebang.
    $ours = $false
    $why = ''
    if ($candidate -ieq $BinCmd) {
        $body = ''
        try { $body = Get-Content -LiteralPath $candidate -Raw } catch { $body = '' }
        if ($body -match [regex]::Escape('..\venv\Scripts\oktografx-server.exe')) {
            $ours = $true; $why = 'it is the shim this installer wrote into the venv'
        }
    } elseif ($Method -eq 'uv') {
        $ours = $true; $why = "install-method says uv, so this is the entry point uv placed in $BinDir"
    }
    if ($ours) {
        Remove-Path2 $candidate
        if (-not $DryRun) { Say "removed $candidate ($why)" }
        $removedAny = $true
    } else {
        Say "left $candidate alone: this script did not create it"
    }
}
if (-not $removedAny) { Say "nothing at $BinDir" }

Log "Removing the environment (method: $Method)"
$uv = Get-Command 'uv' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (($Method -eq 'uv') -and $uv) {
    if ($DryRun) {
        Say '[dry-run] uv tool uninstall okto-grafx-server'
    } elseif ((Invoke-Native 'uv' @('tool', 'uninstall', 'okto-grafx-server')).Code -eq 0) {
        Say 'uv removed its tool environment'
    } else {
        Say 'uv had no such tool installed'
    }
}
$Venv = Join-Path $Prefix 'venv'
if (Test-Path -LiteralPath $Venv) {
    Remove-Path2 $Venv
    if (-not $DryRun) { Say "removed $Venv" }
}
if (Test-Path -LiteralPath $MethodFile) { Remove-Path2 $MethodFile }
if ((Test-Path -LiteralPath $Prefix) -and @(Get-ChildItem -LiteralPath $Prefix -Force -ErrorAction SilentlyContinue).Count -eq 0) {
    Remove-Path2 $Prefix
}

# A daemon holding the control database open is not "removed": stop it first, or the state directory
# cannot be deleted and the operator is left with a half-removed install and no explanation.
$PidFile = Join-Path $StateDir 'daemon.pid'
if (Test-Path -LiteralPath $PidFile -PathType Leaf) {
    # Not `$pid`: PowerShell variable names are case-insensitive, so `$pid` is `$PID`, the read-only
    # automatic variable holding this script's own process id, and assigning to it fails here rather
    # than silently pointing at the wrong process.
    $daemonPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $process = $null
    if ($daemonPid -match '^\d+$') { $process = Get-Process -Id ([int] $daemonPid) -ErrorAction SilentlyContinue }
    if ($process) {
        Log "A daemon is still running from this state directory (pid $daemonPid)"
        if ($DryRun) {
            Say "[dry-run] Stop-Process -Id $daemonPid"
        } else {
            Stop-Process -Id ([int] $daemonPid) -Force -ErrorAction SilentlyContinue
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                if (-not (Get-Process -Id ([int] $daemonPid) -ErrorAction SilentlyContinue)) { break }
                Start-Sleep -Milliseconds 250
            }
            Say "stopped pid $daemonPid"
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        }
    }
}

# PATH is the one thing here that is not a file. Say what is in it rather than editing it, unless asked.
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -and @($userPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $BinDir }).Count -gt 0) {
    if ($RemoveFromPath) {
        $kept = @($userPath -split ';' | Where-Object { $_ -and ($_.TrimEnd('\') -ine $BinDir) }) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $kept, 'User')
        Say "took $BinDir out of your user PATH (open a new terminal for it to apply)"
    } else {
        Say "$BinDir is still in your user PATH. To remove it:"
        Say "  [Environment]::SetEnvironmentVariable('Path', (([Environment]::GetEnvironmentVariable('Path','User') -split ';' | Where-Object { `$_ -and (`$_ -notlike '$BinDir') }) -join ';'), 'User')"
        Say '  or run this uninstaller with -RemoveFromPath'
    }
}

Log 'Left behind on purpose'
if (Test-Path -LiteralPath $StateDir) {
    Say "state directory  $StateDir"
    Say '                 server.toml, server.db, server.log and admin-token: the registrations, API keys'
    Say '                 and audit trail. Every registration is a *reference*, so your Grafx stores are'
    Say '                 untouched wherever they are on disk.'
} else {
    Say "no state directory at $StateDir"
}

if ($PurgeState) {
    Log 'Purging the control plane'
    if (Test-Path -LiteralPath $StateDir) {
        Say "removing $StateDir - the stores it points at are not inside it and are not deleted"
        Remove-Path2 $StateDir
    } else {
        Say 'nothing to remove'
    }
} elseif (Test-Path -LiteralPath $StateDir) {
    Say 'to remove the control plane too, rerun with -PurgeState (registrations, keys, audit, token)'
}

Log 'Done'
$still = Get-Command $Binary -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($still) { Say "$Binary is still on PATH at $($still.Source) - that copy was not made by this script" }
Say "reinstall: curl.exe -fsSL -o install.ps1 https://raw.githubusercontent.com/$Repo/v<tag>/install.ps1"
exit 0
