@echo off
setlocal EnableExtensions

rem Okto Grafx Server - installer front-end for cmd.exe.
rem
rem   curl.exe -fsSL -o install.cmd https://raw.githubusercontent.com/OktoLabsAI/okto-grafx-server/v0.2.0/install.cmd
rem   install.cmd -Port 8899
rem
rem This is not a second installer. The behaviour lives in install.ps1 and this file only finds
rem powershell.exe and hands the arguments over, because two implementations of the same install drift
rem apart and the operator is the one who pays for the drift. Windows has shipped PowerShell on every
rem edition since 7, so the dependency costs nothing; a batch file that verified a SHA-256 with
rem certutil and parsed a TOML would be a second place for the same promise to be wrong.
rem
rem Switches are the PowerShell ones: -Prefix, -StateDir, -Root, -Port, -WithAdminToken, -DryRun, ...
rem Run "install.cmd -Help" for the list.

set "VERSION=0.2.0"
set "REPO=OktoLabsAI/okto-grafx-server"
set "PS1=%~dp0install.ps1"

if "%~1"=="/?" goto :usage
if /i "%~1"=="-h" goto :usage
if /i "%~1"=="--help" goto :usage
if /i "%~1"=="-Help" goto :usage

set "POWERSHELL="
rem Sysnative first: a 32-bit cmd on 64-bit Windows sees System32 as the 32-bit directory, and the
rem 64-bit Windows PowerShell is the one that has been tested.
if exist "%WINDIR%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "POWERSHELL=%WINDIR%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not defined POWERSHELL if exist "%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" set "POWERSHELL=%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not defined POWERSHELL (
    echo error: Windows PowerShell was not found under %WINDIR%. Install it, or run install.ps1 with
    echo        any PowerShell you do have.
    exit /b 1
)

if exist "%PS1%" goto :run
rem The usual case from a cmd prompt: one file was fetched, not two. Put it next to this one so the
rem operator can read it and re-run this front-end without downloading again.
echo install.ps1 was not next to install.cmd, so it is being fetched from the %VERSION% release.
curl.exe -fsSL -o "%PS1%" "https://raw.githubusercontent.com/%REPO%/v%VERSION%/install.ps1"
if errorlevel 1 (
    echo error: could not download install.ps1. Fetch it yourself and check it before running:
    echo        https://raw.githubusercontent.com/%REPO%/v%VERSION%/install.ps1
    exit /b 1
)

:run
"%POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"
exit /b %RC%

:usage
echo Okto Grafx Server installer - cmd front-end
echo.
echo   install.cmd [options]
echo.
echo The options belong to install.ps1 and are PowerShell-style:
echo   -Prefix DIR  -StateDir DIR  -Root DIR  -Port PORT  -Python EXE  -Installer auto^|uv^|pip
echo   -Wheel FILE  -FromTree DIR  -NoInit  -NoRun  -WithAdminToken  -NoTokenFile
echo   -AddToPath   -DryRun   -Help
echo.
echo Or straight into PowerShell, which is the same thing with fewer moving parts:
echo   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Port 8899
exit /b 0
