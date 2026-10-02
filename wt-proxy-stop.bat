@echo off
setlocal EnableExtensions
rem ===========================================================
rem  WorkBuddy + Trae proxy - stopper
rem
rem  Usage: double-click, or   wt-proxy-stop.bat [port]
rem
rem  Default port is 28087 (this copy's port). It does NOT touch
rem  the production instance on 28086 unless you pass it explicitly:
rem       wt-proxy-stop.bat 28086
rem
rem  ASCII-only on purpose: .bat files are read using the console
rem  code page, so non-ASCII text breaks the parser.
rem ===========================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=28087"

echo Stopping WorkBuddy + Trae proxy on port %PORT% ...

set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do call :kill %%P

if not defined FOUND echo No listener found on port %PORT%.
echo Done.
timeout /t 3 >nul
exit /b 0

:kill
set "FOUND=1"
echo   killing PID %1
taskkill /PID %1 /T /F >nul 2>&1
goto :eof