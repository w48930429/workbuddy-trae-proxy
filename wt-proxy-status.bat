@echo off
setlocal EnableExtensions
rem ===========================================================
rem  WorkBuddy + Trae proxy - status
rem
rem  Usage: double-click, or   wt-proxy-status.bat [port]
rem
rem  Default port is 28087 (this copy's port).
rem
rem  ASCII-only on purpose: .bat files are read using the console
rem  code page, so non-ASCII text breaks the parser.
rem ===========================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=28087"

echo ===========================================================
echo   WorkBuddy + Trae proxy - status
echo   port: %PORT%
echo ===========================================================
echo.

set "RUNNING="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do call :show %%P

if not defined RUNNING echo   [STOPPED] no listener on port %PORT%
echo.
echo   dashboard : http://127.0.0.1:%PORT%/
echo   api base  : http://127.0.0.1:%PORT%/v1
echo.
timeout /t 8 >nul
exit /b 0

:show
set "RUNNING=1"
echo   [RUNNING] PID %1
goto :eof