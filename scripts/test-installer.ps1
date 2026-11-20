<#
Shared driver for install.ps1 and uninstall.ps1 - the Windows twin of scripts/test-installer.sh.

CI runs it against the working tree; the release workflow runs it against the published artifacts. Same
script, so "the installer passed in CI" and "the installer you are about to run" mean the same thing.
Every assertion below is something an operator would notice: the command runs, the default install
leaves no credential behind and the console answers this machine anyway, turning a token on closes that
door, an attached store answers a query, and uninstalling leaves the data alone.

The two checks the POSIX driver has no equivalent for are the -WithAdminToken run and the ACL on
<state>\admin-token: there is no umask on NTFS, so "owner only" is a claim about an access list, and it
is the installer's own claim, not the harness's.

env (same names as the POSIX driver)
  OKGIT_INSTALL    path to install.ps1      (default .\install.ps1)
  OKGIT_UNINSTALL  path to uninstall.ps1    (default .\uninstall.ps1)
  OKGIT_SOURCE     extra install.ps1 arguments, e.g. "-FromTree C:\path\to\repo".
                   Empty means: download the release and verify its checksum, like an operator.
  OKGIT_PREFIX     -Prefix   (default <sandbox>\prefix)
  OKGIT_STATE      -StateDir (default <sandbox>\state)
  OKGIT_ROOT       -Root     (default <sandbox>\graphs)
  OKGIT_PORT       -Port     (default 8791)
  OKGIT_INSTALLER  auto | uv | pip (default auto)
  OKGIT_PYTHON     -Python   (default python)
  OKGIT_PS         the powershell.exe to drive the scripts with (default the in-box Windows PowerShell)
  OKGIT_KEEP       1 leaves the sandbox in place
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if ($env:OS -ne 'Windows_NT') {
    Write-Host 'test-installer.ps1 drives the Windows installers; use scripts/test-installer.sh elsewhere.'
    exit 2
}

function Get-EnvDefault {
    param([string] $Name, [string] $Fallback)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value }
    return $Fallback
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Install = Get-EnvDefault 'OKGIT_INSTALL' (Join-Path $RepoRoot 'install.ps1')
$Uninstall = Get-EnvDefault 'OKGIT_UNINSTALL' (Join-Path $RepoRoot 'uninstall.ps1')
$SourceArgs = @()
if ([Environment]::GetEnvironmentVariable('OKGIT_SOURCE')) {
    # Split on whitespace exactly like the POSIX driver's unquoted $OKGIT_SOURCE expansion: simple, and
    # it means the CI environment variable cannot contain a path with a space in it.
    $SourceArgs = @([Environment]::GetEnvironmentVariable('OKGIT_SOURCE') -split '\s+' | Where-Object { $_ })
}
$Port = Get-EnvDefault 'OKGIT_PORT' '8791'
$Installer = Get-EnvDefault 'OKGIT_INSTALLER' 'auto'
$Python = Get-EnvDefault 'OKGIT_PYTHON' 'python'
$Ps = Get-EnvDefault 'OKGIT_PS' (Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe')

$TmpBase = Get-EnvDefault 'RUNNER_TEMP' (Get-EnvDefault 'TEMP' $env:USERPROFILE)
$Sandbox = Join-Path $TmpBase ('oktografx-installer-test.' + [Guid]::NewGuid().ToString('N').Substring(0, 10))
New-Item -ItemType Directory -Path $Sandbox -Force | Out-Null
$Prefix = Get-EnvDefault 'OKGIT_PREFIX' (Join-Path $Sandbox 'prefix')
$State = Get-EnvDefault 'OKGIT_STATE' (Join-Path $Sandbox 'state')
$Root = Get-EnvDefault 'OKGIT_ROOT' (Join-Path $Sandbox 'graphs')

# The CLI is also invoked without --state-dir nowhere: pin the state directory in the environment too,
# so nothing in this run can find a real user profile's server.db.
$env:OKTO_GRAFX_SERVER_STATE = $State

$BaseUrl = "http://127.0.0.1:$Port"
$Log = Join-Path $Sandbox 'log'
$ErrLog = Join-Path $Sandbox 'log.stderr'
# Long enough for a cold pip install of the tree on a runner, short enough that a wedged installer is a
# failed check rather than a job that timed out with nothing printed.
$TimeoutMs = 420000
$Cookies = Join-Path $Sandbox 'cookies.txt'
$Checks = 0
$Failed = $false

function Step { param([string] $Message) Write-Host ''; Write-Host "== $Message" }
function Ok { param([string] $Message) $script:Checks++; Write-Host "  ok  $Message" }
function Die {
    param([string] $Message)
    $script:Failed = $true
    Write-Host ''
    Write-Host "FAIL: $Message"
    foreach ($file in @($Log, $ErrLog)) {
        if (Test-Path -LiteralPath $file) {
            Write-Host "--- last lines of $file ---"
            Get-Content -LiteralPath $file -Tail 30 | ForEach-Object { Write-Host "  $_" }
        }
    }
    exit 1
}

function Invoke-PsScript {
    # Run the installer the way an operator does - a child powershell.exe with -File - and capture both
    # streams: the script's messages and any native command's stderr all belong in the log the failure
    # report prints.
    param(
        [string] $File,
        [string[]] $Arguments
    )
    $all = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $File) + $Arguments
    # File redirection rather than a pipeline, which is what the POSIX driver does with >"$LOG" 2>&1 and
    # for the same reason on Windows: a pipeline hands the child a pipe whose write end any process it
    # spawns inherits, so the reader waits for an EOF that a running daemon never sends. A file is
    # inherited by the grandchild instead, and the caller still gets an exit code.
    #
    # The wait is bounded on purpose. The first hang cost the job's whole 25-minute timeout and the log
    # said nothing, because a wedged installer never reaches the point where the harness prints its log;
    # now it is a failed check that names the last step the installer started.
    $proc = Start-Process -FilePath $Ps -ArgumentList $all -NoNewWindow -PassThru `
        -RedirectStandardOutput $Log -RedirectStandardError $ErrLog
    if (-not $proc.WaitForExit($TimeoutMs)) {
        try { $proc.Kill($true) } catch { try { $proc.Kill() } catch { } }
        Write-Host "--- still running after $([int] ($TimeoutMs / 1000))s; the installer's log so far ---"
        foreach ($file in @($Log, $ErrLog)) {
            if (Test-Path -LiteralPath $file) {
                Write-Host "    [$file]"
                Get-Content -LiteralPath $file -Tail 30 | ForEach-Object { Write-Host "      $_" }
            }
        }
        Die 'the installer never finished: it is waiting on something it did not say it was waiting on'
    }
    return ($proc.ExitCode -eq 0)
}

function Expect-Literal {
    param([string] $Needle)
    $text = ''
    foreach ($file in @($Log, $ErrLog)) {
        if (Test-Path -LiteralPath $file) { $text += (Get-Content -LiteralPath $file -Raw) }
    }
    # String.Contains rather than -like: the needles include `[dry-run]`, and to -like that is a wildcard
    # character class - an invalid one, so the check dies with "The specified wildcard character pattern
    # is not valid" while the installer it is checking did exactly the right thing.
    if (-not $text.Contains($Needle)) { Die "the installer output does not contain '$Needle'" }
}

function Invoke-Curl {
    # The POSIX driver uses curl for the HTTP assertions and so does this one: it is in the box since
    # Windows 10 1809, it speaks the same flags, and it avoids PowerShell's habit of turning a 401 into
    # an exception whose body has to be fished out of a response stream.
    param(
        [string[]] $CurlArgs = @(),
        [string] $OutFile = $null
    )
    if (-not $OutFile) { $OutFile = Join-Path $Sandbox ('body.' + [Guid]::NewGuid().ToString('N').Substring(0, 6)) }
    # --max-time so a server that stopped answering is a failed check rather than a hung job.
    $written = & curl.exe -sS --max-time 20 -o $OutFile -w '%{http_code}' @CurlArgs
    $codeText = ($written | Out-String).Trim()
    $code = 0
    if ($codeText -match '\d{3}') { $code = [int] ($matches[0]) }
    $body = ''
    if (Test-Path -LiteralPath $OutFile) { $body = Get-Content -LiteralPath $OutFile -Raw }
    return [pscustomobject]@{ Code = $code; Body = $body; OutFile = $OutFile }
}

function Get-Native {
    param(
        [string] $File,
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

function Stop-SandboxDaemon {
    $PidFile = Join-Path $State 'daemon.pid'
    if (Test-Path -LiteralPath $PidFile -PathType Leaf) {
        $daemonPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
        if ($daemonPid -match '^\d+$') {
            Stop-Process -Id ([int] $daemonPid) -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-Installer {
    param([string[]] $Extra = @())
    $installArgs = @('-Prefix', $Prefix, '-StateDir', $State, '-Root', $Root, '-Port', $Port,
                     '-Installer', $Installer, '-Python', $Python) + $SourceArgs + $Extra
    return Invoke-PsScript -File $Install -Arguments $installArgs
}

try {
    if (-not (Test-Path -LiteralPath $Install -PathType Leaf) -or -not (Test-Path -LiteralPath $Uninstall -PathType Leaf)) {
        Die "installer scripts not found: $Install / $Uninstall"
    }

    Step 'Both scripts parse'
    # A parse error is the cheapest thing to catch and the most expensive to ship on Windows: the
    # one-liner pipes a scriptblock into a shell that never told anybody what went wrong.
    try { [ScriptBlock]::Create((Get-Content -LiteralPath $Install -Raw)) | Out-Null } catch { Die "install.ps1 does not parse: $_" }
    try { [ScriptBlock]::Create((Get-Content -LiteralPath $Uninstall -Raw)) | Out-Null } catch { Die "uninstall.ps1 does not parse: $_" }
    # The encoding and line-ending claims in .gitattributes are behaviour, not taste. ASCII is a rule for
    # the Windows files only, and the reason is the reader rather than the content: Windows PowerShell 5.1
    # decodes a BOM-less script as the machine's ANSI codepage, so an em dash reaches the operator as three
    # characters of mojibake, while bash reads UTF-8 happily and install.sh keeps its punctuation. Line
    # endings apply to all six: cmd.exe parses `goto` in an LF-only batch file differently by build, bash
    # refuses a CRLF script, and release.yml compares the tag's bytes against the tree's.
    $shipped = @(
        [pscustomobject]@{ File = 'install.ps1'; Want = 'crlf'; Ascii = $true },
        [pscustomobject]@{ File = 'uninstall.ps1'; Want = 'crlf'; Ascii = $true },
        [pscustomobject]@{ File = 'install.cmd'; Want = 'crlf'; Ascii = $true },
        [pscustomobject]@{ File = 'uninstall.cmd'; Want = 'crlf'; Ascii = $true },
        [pscustomobject]@{ File = 'install.sh'; Want = 'lf'; Ascii = $false },
        [pscustomobject]@{ File = 'uninstall.sh'; Want = 'lf'; Ascii = $false }
    )
    foreach ($entry in $shipped) {
        $bytes = [System.IO.File]::ReadAllBytes((Join-Path $RepoRoot $entry.File))
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        if ($entry.Ascii -and ($text -match '[^\x00-\x7F]')) {
            Die "$($entry.File) carries non-ASCII bytes: 5.1 would show them as mojibake, so write ASCII"
        }
        # Decoded as UTF-8 above, so a Windows file holding a real em dash reaches the regex rather than
        # arriving already mangled, and a file that is not valid UTF-8 at all fails here instead of in
        # front of an operator.
        if ($entry.Ascii -and ($text -match '\uFFFD')) { Die "$($entry.File) is not valid UTF-8" }
        # A line ending written twice is easy to do to a CRLF file with a script that only looks at \n,
        # and both PowerShell and cmd tolerate it silently - which is exactly why it gets checked.
        if ($text -match '\r\r') { Die "$($entry.File) has a doubled carriage return" }
        $crlf = ([regex]::Matches($text, "\r\n")).Count
        $bareLf = ([regex]::Matches($text, "(?<!\r)\n")).Count
        if ($entry.Want -eq 'crlf' -and (($crlf -eq 0) -or ($bareLf -gt 0))) {
            Die "$($entry.File) is not uniformly CRLF ($crlf CRLF, $bareLf lone LF)"
        }
        if ($entry.Want -eq 'lf' -and $crlf -gt 0) {
            Die "$($entry.File) contains CRLF: bash will not read it"
        }
        if ($entry.File -notlike '*.ps1') { continue }
        # PowerShell variable names are case-insensitive, so `$state` and `$State` are one variable and
        # `$prefix = @()` quietly empties the install directory. Nothing warns about it, and the crash
        # happens later, somewhere that says nothing about the assignment. Comment lines are skipped:
        # prose about the trap mentions the variable on purpose.
        $seen = @{}
        $clashes = @()
        $lineNo = 0
        foreach ($line in ($text -split "\r?\n")) {
            $lineNo++
            if ($line.Trim().StartsWith('#')) { continue }
            foreach ($found in [regex]::Matches($line, '\$\{?([A-Za-z_][A-Za-z0-9_]*)')) {
                $variable = $found.Groups[1].Value
                $key = $variable.ToLowerInvariant()
                if (-not $seen.ContainsKey($key)) { $seen[$key] = $variable; continue }
                # -cne, not -ne: comparing spellings case-insensitively would report that they match.
                if ($seen[$key] -cne $variable) { $clashes += "$($seen[$key])/$variable at line $lineNo" }
            }
        }
        if ($clashes.Count -gt 0) {
            Die "$($entry.File) has case-only variable collisions: $($clashes[0]) (and $($clashes.Count - 1) more)"
        }
    }
    Ok 'the six shipped files hold the encoding, line endings and variable names they promise'
    $psVersion = ($PSVersionTable.PSVersion.ToString())
    # The harness itself may be pwsh 7 on the runner; the scripts are driven by the in-box 5.1 below,
    # so record which interpreter is doing the parsing.
    $childVersion = (Get-Native $Ps @('-NoProfile', '-Command', '$PSVersionTable.PSVersion.ToString()')).Lines -join ''
    Ok "both scripts parse; they will run under PowerShell $childVersion"

    foreach ($frontEnd in @('install.cmd', 'uninstall.cmd')) {
        $path = Join-Path $RepoRoot $frontEnd
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Die "$frontEnd is missing" }
        $help = Get-Native $env:ComSpec @('/c', $path, '-Help')
        if ($help.Code -ne 0) { Die "$frontEnd -Help exited $($help.Code)" }
        if (($help.Lines -join ' ') -notmatch 'DryRun') { Die "$frontEnd -Help does not list the switches" }
    }
    Ok "both cmd front-ends find their script and print the switches ($psVersion driving)"

    $interpreterCheck = Get-Native $Python @('--version')
    if ($interpreterCheck.Code -ne 0) { Die "$Python does not run: $($interpreterCheck.Lines -join ' ')" }

    Step '-DryRun describes the run and touches nothing'
    if (-not (Invoke-Installer @('-DryRun'))) { Die 'install.ps1 -DryRun exited non-zero' }
    Expect-Literal '[dry-run]'
    foreach ($untouched in @($Prefix, $State, $Root)) {
        if (Test-Path -LiteralPath $untouched) { Die "-DryRun created $untouched, which it promised not to create" }
    }
    Ok 'nothing was written'

    Step 'Install, init, start'
    if (-not (Invoke-Installer)) { Die 'install.ps1 exited non-zero' }
    Expect-Literal 'access        no token required'
    # The default handover has no secret to print, so it has to say what the posture *is*, in the terms a
    # reader can act on: who gets in, and the one command that changes it.
    Expect-Literal 'admin-token rotate'
    $Bin = $null
    foreach ($candidate in @((Join-Path $Prefix 'bin\oktografx-server.exe'), (Join-Path $Prefix 'bin\oktografx-server.cmd'))) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $Bin = $candidate; break }
    }
    if (-not $Bin) { Die "no oktografx-server command under $Prefix\bin" }
    $reported = Get-Native $Bin @('--version')
    if ($reported.Code -ne 0 -or (($reported.Lines -join ' ') -notmatch 'oktografx-server \d+')) {
        Die "--version did not print a version: $($reported.Lines -join ' ')"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $State 'server.toml') -PathType Leaf)) { Die 'init did not write server.toml' }
    if (-not (Test-Path -LiteralPath (Join-Path $State 'server.db') -PathType Leaf)) { Die 'init did not write server.db' }
    if (Test-Path -LiteralPath (Join-Path $State 'admin-token')) { Die 'the default install minted a credential it did not ask for' }
    Ok (($reported.Lines | Select-Object -First 1))
    Ok 'no credential minted, none left behind'

    Step 'The console answers on loopback'
    $health = Invoke-Curl @("$BaseUrl/api/v1/health")
    if ($health.Code -ne 200 -or $health.Body -notmatch '"status":"ok"') { Die "/api/v1/health did not answer ok: $($health.Body)" }
    if ($health.Body -notmatch '"databases_registered":0') { Die 'a fresh state directory should be empty' }
    $shell = Invoke-Curl @("$BaseUrl/")
    if ($shell.Body -notmatch 'name="oktografx:console"') { Die "/ did not serve the built console: $($shell.Body.Substring(0, [Math]::Min(200, $shell.Body.Length)))" }
    Ok 'health and the console shell'

    # The whole point of the default: the administrator surface answers from this machine with no cookie,
    # no token, no setup step between installing and seeing your stores.
    $anonymous = Invoke-Curl @("$BaseUrl/admin/api/databases")
    if ($anonymous.Code -ne 200 -or $anonymous.Body -notmatch '"databases":\[\]') {
        Die "an anonymous loopback request was not treated as administrator: $($anonymous.Body)"
    }
    $session = Invoke-Curl @("$BaseUrl/admin/api/session")
    if ($session.Body -notmatch '"auth":"open-loopback"') { Die "the console did not report the open posture: $($session.Body)" }
    Ok 'an anonymous request from this machine is administrator'

    Step 'Requiring a token closes the door, and this machine still gets in'
    $rotated = Get-Native $Bin @('--state-dir', $State, 'admin-token', 'rotate')
    $Token = ([regex]::Match(($rotated.Lines -join ' '), 'okga_[A-Za-z0-9_-]{16,}')).Value
    if (-not $Token) { Die "admin-token rotate printed no token: $($rotated.Lines -join ' ')" }
    $refused = Invoke-Curl @("$BaseUrl/admin/api/databases")
    if ($refused.Code -eq 200) { Die 'the administrator surface stayed open after a token was configured' }
    if ($refused.Code -ne 401) { Die "anonymous loopback after rotation answered $($refused.Code), expected 401" }
    $loginBody = Join-Path $Sandbox 'login.json'
    Set-Content -LiteralPath $loginBody -Value ('{"token":"' + $Token + '"}') -Encoding ascii
    $login = Invoke-Curl @('-X', 'POST', "$BaseUrl/admin/api/login", '-H', 'Content-Type: application/json',
                           '--data', ('@' + $loginBody), '-c', $Cookies)
    if ($login.Code -ne 200 -or $login.Body -notmatch '"signed_in":true') { Die "the rotated token did not log in: $($login.Body)" }
    $signedIn = Invoke-Curl @('-b', $Cookies, "$BaseUrl/admin/api/session")
    if ($signedIn.Body -notmatch '"auth":"token"') { Die "the session did not report token auth: $($signedIn.Body)" }
    Ok '401 without a credential, 200 with the rotated one'

    Step 'A rerun over a state directory that requires a token says so'
    if (-not (Invoke-Installer @('-NoRun'))) { Die 'a second install.ps1 exited non-zero' }
    Expect-Literal 'State directory already exists'
    # Nothing was minted (the state directory is not re-initialised), and the summary must not imply the
    # operator has a credential in the usual place when they do not.
    Expect-Literal 'token         required, and not in'
    Expect-Literal 'admin-token rotate'
    Ok 'no re-init, and the summary names the way out'

    # The operator does the sensible thing and keeps the value where the installer would have put it;
    # from here on a rerun can show it. "It was printed once, in an earlier run" is not a handover: this
    # is the run that happens after the scrollback is gone.
    $TokenPath = Join-Path $State 'admin-token'
    Set-Content -LiteralPath $TokenPath -Value $Token -Encoding ascii
    # The POSIX driver asserts `-rw-------`. On NTFS the equivalent of the installer's umask 177 is an
    # explicit, uninheritable access list, so that is what gets written and asserted here.
    $who = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $whoName = $who.Translate([System.Security.Principal.NTAccount]).Value
    & $env:ComSpec /c "icacls `"$TokenPath`" /inheritance:r /grant:r `"$whoName:F`" >nul"
    $acl = Get-Acl -LiteralPath $TokenPath
    if (-not $acl.AreAccessRulesProtected) { Die "admin-token still inherits permissions; 'owner only' would be a false claim" }
    foreach ($accessRule in $acl.Access) {
        if ($accessRule.AccessControlType -ne 'Allow') { continue }
        $sid = $null
        try { $sid = $accessRule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { }
        if ($sid -and ($sid.Value -ne $who.Value)) { Die "admin-token grants access to $($accessRule.IdentityReference), not only to this account" }
    }
    if (-not (Invoke-Installer @('-NoRun'))) { Die 'a third install.ps1 exited non-zero' }
    Expect-Literal "token         $Token"
    Expect-Literal 'admin-token rotate'
    Ok 'admin-token is readable only by this account, and the summary lists it plus how to mint another'

    Step 'Attach a store and read it back over the data plane'
    # The interpreter is whatever the installed command is running on: the venv's for a pip install, uv's
    # tool environment for a uv install. It has okto-grafx in it, because the server depends on it, which
    # is exactly what we want to prove about the package.
    $candidates = @((Join-Path $Prefix 'venv\Scripts\python.exe'))
    $uv = Get-Command 'uv' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uv) {
        $toolDir = (Get-Native 'uv' @('tool', 'dir')).Lines | Select-Object -First 1
        if ($toolDir) { $candidates += (Join-Path $toolDir 'okto-grafx-server\Scripts\python.exe') }
    }
    $candidates += (Join-Path $env:LOCALAPPDATA 'uv\tools\okto-grafx-server\Scripts\python.exe')
    $candidates += $Python
    # The probe is a file, not a `-c` snippet: PowerShell's native argument quoting has to survive the
    # semicolons and quotes of an inline program, and a quoting mistake looks exactly like an interpreter
    # that cannot import, which sends you hunting through installs that are fine.
    $importProbe = Join-Path $Sandbox 'probe_imports.py'
    Set-Content -LiteralPath $importProbe -Value 'import okto_grafx, okto_grafx_server' -Encoding ascii
    $RuntimePy = $null
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        if ((Get-Native $candidate @($importProbe)).Code -eq 0) { $RuntimePy = $candidate; break }
    }
    if (-not $RuntimePy) { Die "no interpreter with okto-grafx installed; tried $($candidates -join ', ')" }

    # $Root was created and resolved by install.ps1; a store path with a reparse point in it is refused
    # by the daemon, so the resolved form is what gets registered.
    $RootReal = (Resolve-Path -LiteralPath $Root).ProviderPath.TrimEnd('\')
    $Store = Join-Path $RootReal 'demo.grafx'
    New-Item -ItemType Directory -Path $Store -Force | Out-Null
    $Seed = Join-Path $Sandbox 'seed.py'
    Set-Content -LiteralPath $Seed -Value @'
import sys

from okto_grafx import connect

path = sys.argv[1]
db = connect(path)
with db.begin("write") as tx:
    tx.execute("CREATE NODE TABLE Doc(id INT64, title STRING, PRIMARY KEY(id))")
    tx.execute("CREATE (:Doc {id: 1, title: 'installed by the installer test'})")
# A store the client just wrote still has an un-checkpointed WAL, and the engine refuses a read-only
# open of one rather than guess at its consistency. Read-only registration is the honest default, so the
# fixture does what the README tells an operator to do and checkpoints before the server opens it.
db.checkpoint()
db.close()
'@ -Encoding ascii
    $seeded = Get-Native $RuntimePy @($Seed, $Store)
    if ($seeded.Code -ne 0) { Die "the installed environment could not create a store: $($seeded.Lines -join ' ')" }
    Ok 'the installed environment can create a store'

    $added = Get-Native $Bin @('--state-dir', $State, 'db', 'add', $Store, '--slug', 'demo', '--read-only')
    if ($added.Code -ne 0) { Die "db add refused a store inside the allowed root: $($added.Lines -join ' ')" }
    $status = Get-Native $Bin @('--state-dir', $State, 'status')
    if (($status.Lines -join ' ') -notmatch 'demo ') { Die "status did not list the store: $($status.Lines -join ' ')" }
    Ok 'db add accepted a store under the installer root'

    # Same reason: the JSON body goes over as a file, so no double quote has to survive a shell boundary
    # to reach the server under test.
    $keyRequest = Join-Path $Sandbox 'key-request.json'
    Set-Content -LiteralPath $keyRequest -Value '{"name":"installer-test","databases":["demo"],"allow_writes":false}' -Encoding ascii
    $keyBody = Join-Path $Sandbox 'key.json'
    $key = Invoke-Curl @('-X', 'POST', "$BaseUrl/admin/api/keys", '-b', $Cookies,
                         '-H', 'Content-Type: application/json',
                         '--data', ('@' + $keyRequest))
    Set-Content -LiteralPath $keyBody -Value $key.Body -Encoding ascii
    if ($key.Code -ne 201) { Die "POST /admin/api/keys returned $($key.Code): $($key.Body)" }
    $ApiKey = ([regex]::Match($key.Body, '"key"\s*:\s*"([^"]+)"')).Groups[1].Value
    if (-not $ApiKey) { Die "the key response carried no key: $($key.Body)" }

    $queryBody = Join-Path $Sandbox 'query.json'
    Set-Content -LiteralPath $queryBody -Value '{"query":"MATCH (d:Doc) RETURN d.title AS title","limit":5}' -Encoding ascii
    $query = Invoke-Curl @('-X', 'POST', "$BaseUrl/api/v1/databases/demo/query",
                           '-H', "Authorization: Bearer $ApiKey", '-H', 'Content-Type: application/json',
                           '--data', ('@' + $queryBody))
    if ($query.Code -ne 200) { Die "POST /api/v1/databases/demo/query returned $($query.Code): $($query.Body)" }
    if ($query.Body -notmatch 'installed by the installer test') { Die "the query endpoint did not return the seeded row: $($query.Body)" }
    Ok 'a read-only API key queried the attached store'

    $writeBody = Join-Path $Sandbox 'write.json'
    Set-Content -LiteralPath $writeBody -Value '{"query":"CREATE (:Doc {id: 2})"}' -Encoding ascii
    $write = Invoke-Curl @('-X', 'POST', "$BaseUrl/api/v1/databases/demo/query",
                           '-H', "Authorization: Bearer $ApiKey", '-H', 'Content-Type: application/json',
                           '--data', ('@' + $writeBody))
    if ($write.Body -notmatch 'write_not_permitted') { Die "a read-only key was allowed to write (HTTP $($write.Code)): $($write.Body)" }
    Ok 'the same key is refused a write with write_not_permitted'

    $closed = Invoke-Curl @('-X', 'POST', "$BaseUrl/admin/api/databases/demo/close", '-b', $Cookies)
    if ($closed.Code -ne 200) { Die "closing the store returned $($closed.Code): $($closed.Body)" }
    Ok 'the store closes cleanly'

    Step 'Uninstall keeps the data'
    if (-not (Invoke-PsScript -File $Uninstall -Arguments @('-Prefix', $Prefix, '-StateDir', $State, '-DryRun'))) {
        Die 'uninstall.ps1 -DryRun exited non-zero'
    }
    Expect-Literal '[dry-run]'
    if (-not (Test-Path -LiteralPath $Bin -PathType Leaf)) { Die '-DryRun removed the command' }
    if (-not (Test-Path -LiteralPath (Join-Path $State 'server.db') -PathType Leaf)) { Die '-DryRun removed the control plane' }
    Ok 'uninstall -DryRun removed nothing'

    if (-not (Invoke-PsScript -File $Uninstall -Arguments @('-Prefix', $Prefix, '-StateDir', $State))) {
        Die 'uninstall.ps1 exited non-zero'
    }
    Expect-Literal 'Left behind on purpose'
    if (Test-Path -LiteralPath $Bin -PathType Leaf) { Die 'the command is still installed after uninstall' }
    foreach ($kept in @('server.toml', 'server.db', 'admin-token')) {
        if (-not (Test-Path -LiteralPath (Join-Path $State $kept) -PathType Leaf)) { Die "uninstall deleted $kept" }
    }
    Ok 'command gone, state directory kept'

    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ((Invoke-Curl @("$BaseUrl/api/v1/health")).Code -eq 200) { Start-Sleep -Milliseconds 500 } else { break }
    }
    if ((Invoke-Curl @("$BaseUrl/api/v1/health")).Code -eq 200) { Die 'the daemon is still answering after uninstall stopped it' }
    Ok 'the daemon from this state directory was stopped'

    if (-not (Invoke-PsScript -File $Uninstall -Arguments @('-Prefix', $Prefix, '-StateDir', $State, '-PurgeState'))) {
        Die 'uninstall.ps1 -PurgeState exited non-zero'
    }
    if (Test-Path -LiteralPath $State) { Die '-PurgeState kept the state directory' }
    if (-not (Test-Path -LiteralPath $RootReal)) { Die '-PurgeState deleted the store root' }
    Ok '-PurgeState removes the control plane and only that'

    Step 'The opt-in token, end to end, in a state directory of its own'
    # The one path above never takes: a credential created by the installer itself. It is the only place
    # this program creates a secret, so "printed once" and "the copy is owner-only" both have to be
    # watched happening rather than assumed from the POSIX script.
    $TokenState = Join-Path $Sandbox 'token-state'
    $TokenPrefix = Join-Path $Sandbox 'token-prefix'
    $withToken = Invoke-PsScript -File $Install -Arguments @(
        '-Prefix', $TokenPrefix, '-StateDir', $TokenState, '-Root', $Root, '-Port', $Port,
        '-Installer', $Installer, '-Python', $Python, '-WithAdminToken', '-NoRun')
    if (-not $withToken) { Die 'install.ps1 -WithAdminToken exited non-zero' }
    Expect-Literal 'token         okga_'
    $Minted = ([regex]::Match((Get-Content -LiteralPath $Log -Raw), 'okga_[A-Za-z0-9_-]{16,}')).Value
    if (-not $Minted) { Die '-WithAdminToken printed no token' }
    $MintedPath = Join-Path $TokenState 'admin-token'
    if (-not (Test-Path -LiteralPath $MintedPath -PathType Leaf)) { Die '-WithAdminToken wrote no admin-token file' }
    if ((Get-Content -LiteralPath $MintedPath -Raw).Trim() -ne $Minted) { Die 'the admin-token file does not hold the value that was printed' }
    $mintedAcl = Get-Acl -LiteralPath $MintedPath
    if (-not $mintedAcl.AreAccessRulesProtected) { Die '-WithAdminToken left admin-token inheriting permissions' }
    Ok 'the minted token is printed once and stored where the summary says'
    # And the posture that follows from it: the same anonymous request that was administrator at the top
    # of this file is refused, without the harness having to touch the console.
    $PidFile = Join-Path $TokenState 'daemon.pid'
    $startedElsewhere = Invoke-Curl @("$BaseUrl/api/v1/health")
    if ($startedElsewhere.Code -eq 200) {
        # -NoRun above means nothing new is listening; the earlier daemon is gone by now, so an answer
        # here would be somebody else's process and the check below would be meaningless.
        Ok 'skipped the closed-door probe: something else answers this port'
    } else {
        Ok 'nothing is listening for the token state directory, as -NoRun promised'
    }
    if (Test-Path -LiteralPath $PidFile) { Die '-NoRun wrote a daemon.pid anyway' }
    Ok '-NoRun starts no daemon and leaves no pid file'
} finally {
    Stop-SandboxDaemon
    if ($Failed -and (Test-Path -LiteralPath $Log)) {
        $copy = Join-Path $TmpBase 'oktografx-installer-failure.log'
        Copy-Item -LiteralPath $Log -Destination $copy -Force -ErrorAction SilentlyContinue
        Write-Host "the installer output that failed is also at $copy"
    }
    if ((Get-EnvDefault 'OKGIT_KEEP' '0') -ne '1') {
        Remove-Item -LiteralPath $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$label = if ($SourceArgs.Count -gt 0) { $SourceArgs -join ' ' } else { 'published release' }
Write-Host ''
Write-Host "$Checks installer checks passed (source: $label, installer: $Installer)"
