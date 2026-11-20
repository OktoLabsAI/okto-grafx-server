@echo off
setlocal EnableExtensions

rem Okto Grafx Server - uninstaller front-end for cmd.exe.
rem
rem   curl.exe -fsSL -o uninstall.cmd https://raw.githubusercontent.com/OktoLabsAI/okto-grafx-server/v0.2.0/uninstall.cmd
rem   uninstall.cmd
rem
rem A front-end, not a second implementation: the behaviour is uninstall.ps1 and this finds
rem powershell.exe and hands the arguments over. Switches belong to that file: -Prefix, -StateDir,
rem -PurgeState, -RemoveFromPath, -DryRun, -Help.

set "VERSION=0.2.0"
set "REPO=OktoLabsAI/okto-grafx-server"
set "PS1=%~dp0uninstall.ps1"

if "%~1"=="/?" goto :usage
if /i "%~1"=="-h" goto :usage
if /i "%~1"=="--help" goto :usage
if /i "%~1"=="-Help" goto :usage

set "POWERSHELL="
if exist "%WINDIR%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "POWERSHELL=%WINDIR%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not defined POWERSHELL if exist "%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" set "POWERSHELL=%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not defined POWERSHELL (
    echo error: Windows PowerShell was not found under %WINDIR%. Install it, or run uninstall.ps1 with
    echo        any PowerShell you do have.
    exit /b 1
)

if exist "%PS1%" goto :run
echo uninstall.ps1 was not next to uninstall.cmd, so it is being fetched from the %VERSION% release.
curl.exe -fsSL -o "%PS1%" "https://raw.githubusercontent.com/%REPO%/v%VERSION%/uninstall.ps1"
if errorlevel 1 (
    echo error: could not download uninstall.ps1. Fetch it yourself:
    echo        https://raw.githubusercontent.com/%REPO%/v%VERSION%/uninstall.ps1
    exit /b 1
)

:run
"%POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"
exit /b %RC%

:usage
echo Okto Grafx Server uninstaller - cmd front-end
echo.
echo   uninstall.cmd [options]
echo.
echo The options belong to uninstall.ps1:
echo   -Prefix DIR   -StateDir DIR   -PurgeState   -RemoveFromPath   -DryRun   -Help
echo.
echo It removes the command and its environment, and leaves the state directory, the registrations and
echo every Grafx store alone unless you pass -PurgeState.
exit /b 0
