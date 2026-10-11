@echo off
setlocal

REM ============================================================
REM  omp launcher (oh-my-pi coding agent) - CONTINUE previous session
REM    1. double-click this bat        -> continue last session in current dir
REM    2. drag a folder onto this bat  -> continue last session in that dir
REM    3. command line: omp-yolo-c.bat <workdir>
REM  Uses: omp --yolo --continue
REM ============================================================

REM workdir: drag-drop arg first, else current dir
if "%~1"=="" (
    set "WORKDIR=%CD%"
) else (
    set "WORKDIR=%~1"
)

REM find omp: prefer local install, then PATH omp.exe, then bun shim omp.cmd
REM (never use bare 'where omp' - it would match this omp.bat launcher itself)
set "OMP_EXE="
if exist "E:\ProgramFiles\omp\omp.exe" set "OMP_EXE=E:\ProgramFiles\omp\omp.exe"
if not defined OMP_EXE if exist "%LOCALAPPDATA%\omp\omp.exe" set "OMP_EXE=%LOCALAPPDATA%\omp\omp.exe"
if not defined OMP_EXE for /f "delims=" %%i in ('where omp.exe 2^>nul') do ( set "OMP_EXE=%%i" & goto :found )
if not defined OMP_EXE for /f "delims=" %%i in ('where omp.cmd 2^>nul') do ( set "OMP_EXE=%%i" & goto :found )
:found
if not defined OMP_EXE (
    echo.
    echo [ERROR] omp not found. Install it first:
    echo.
    echo   In PowerShell:
    echo   $env:HTTPS_PROXY="http://127.0.0.1:20081"; $env:HTTP_PROXY="http://127.0.0.1:20081"
    echo   irm https://omp.sh/install.ps1 ^| iex
    echo.
    pause
    exit /b 1
)

cd /d "%WORKDIR%" 2>nul
if errorlevel 1 (
    echo [ERROR] cannot enter dir: %WORKDIR%
    pause
    exit /b 1
)

REM keep the folder title while omp's terminal UI is running
for %%i in ("%CD%") do set "CURDIRNAME=%%~nxi"
if not defined CURDIRNAME set "CURDIRNAME=%CD%"
set "OMP_WINDOW_TITLE=omp-c - %CURDIRNAME%"
title %OMP_WINDOW_TITLE%
start "" /b powershell.exe -NoLogo -NoProfile -Command "$parentId = (Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID)).ParentProcessId; while (Get-Process -Id $parentId -ErrorAction SilentlyContinue) { [Console]::Title = $env:OMP_WINDOW_TITLE; Start-Sleep -Milliseconds 250 }" >nul 2>nul

echo.
echo Continuing omp session  (workdir: %CD%)
echo Session is stored per-directory.
echo Approval mode: --yolo (auto-approve all tiers; critical cmds still confirm)
echo ============================================================
REM --yolo : auto-approve all tool tiers (approvalMode=yolo)
REM          critical bash cmds (e.g. rm -rf /) still confirm - hard guard
REM --continue : resume the most recent session in this directory
"%OMP_EXE%" --yolo --continue

if errorlevel 1 (
    echo.
    echo [omp exited with code %errorlevel%]
    pause
)
endlocal
