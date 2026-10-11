@echo off
setlocal EnableExtensions
rem ===========================================================
rem  WorkBuddy + Trae proxy - All-in-one background launcher & guardian
rem
rem  Usage:
rem    wt-proxy-start.bat                (start or manage default port 28087)
rem    wt-proxy-start.bat [port]         (start or manage specified port)
rem    wt-proxy-start.bat start [port]   (start in background with guardian)
rem    wt-proxy-start.bat stop [port]    (stop guardian & proxy completely)
rem    wt-proxy-start.bat restart [port] (restart service)
rem    wt-proxy-start.bat status [port]  (check status)
rem
rem  ASCII-only on purpose: .bat files are read using the
rem  console code page, so non-ASCII text breaks the parser.
rem  No %VAR% inside if(...) blocks to avoid cmd.exe parsing bugs.
rem ===========================================================

set "HERE=%~dp0"
set "SCRIPT=%HERE%wb_proxy.py"

if not exist "%SCRIPT%" goto :no_script

rem ---- Parse Command & Port Arguments ----
set "CMD=%~1"
set "ARG2=%~2"
set "PORT=28087"

if "%CMD%"=="" goto :detect_default
if /i "%CMD%"=="--worker" goto :action_worker
if /i "%CMD%"=="stop" goto :action_stop_arg
if /i "%CMD%"=="start" goto :action_start_arg
if /i "%CMD%"=="restart" goto :action_restart_arg
if /i "%CMD%"=="status" goto :action_status_arg

rem If %CMD% is a number or other value, treat it as PORT
set "PORT=%CMD%"
goto :detect_default

:action_stop_arg
if not "%ARG2%"=="" set "PORT=%ARG2%"
goto :do_stop

:action_start_arg
if not "%ARG2%"=="" set "PORT=%ARG2%"
goto :do_start

:action_restart_arg
if not "%ARG2%"=="" set "PORT=%ARG2%"
goto :do_restart

:action_status_arg
if not "%ARG2%"=="" set "PORT=%ARG2%"
goto :do_status

:detect_default
call :check_port_listening %PORT%
if "%PORT_LISTENING%"=="1" goto :interactive_menu
goto :do_start

rem ===========================================================
rem  Actions
rem ===========================================================

:action_worker
if not "%ARG2%"=="" set "PORT=%ARG2%"
set "STOPFILE=%HERE%wt-proxy-%PORT%.stop"
set "PIDFILE=%HERE%wt-proxy-%PORT%.pid"
set "LOGFILE=%HERE%wt-proxy-%PORT%.out.log"
set "ERRFILE=%HERE%wt-proxy-%PORT%.err.log"

call :find_python
if not defined PYEXE exit /b 1

if exist "%STOPFILE%" del "%STOPFILE%" >nul 2>&1

:worker_loop
if exist "%STOPFILE%" goto :worker_exit
"%PYEXE%" "%SCRIPT%" --port %PORT% >> "%LOGFILE%" 2>> "%ERRFILE%"
if exist "%STOPFILE%" goto :worker_exit
rem Auto-healing delay before revival
ping -n 3 127.0.0.1 >nul
goto :worker_loop

:worker_exit
if exist "%STOPFILE%" del "%STOPFILE%" >nul 2>&1
if exist "%PIDFILE%" del "%PIDFILE%" >nul 2>&1
exit /b 0

:do_stop
call :sub_stop
exit /b 0

:do_restart
call :sub_stop
ping -n 3 127.0.0.1 >nul
goto :do_start

:sub_stop
set "STOPFILE=%HERE%wt-proxy-%PORT%.stop"
set "PIDFILE=%HERE%wt-proxy-%PORT%.pid"

echo ===========================================================
echo   Stopping WorkBuddy + Trae proxy on port %PORT% ...
echo ===========================================================

rem 1. Set stop flag to prevent guardian worker loop from restarting
echo stop > "%STOPFILE%"

rem 2. Kill guardian worker process tree
if exist "%PIDFILE%" (
  for /f "usebackq" %%P in ("%PIDFILE%") do (
    taskkill /PID %%P /T /F >nul 2>&1
  )
  del "%PIDFILE%" >nul 2>&1
)

rem 3. Kill port listeners
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
  taskkill /PID %%P /T /F >nul 2>&1
)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr "127.0.0.1:%PORT% " ^| findstr "LISTENING"') do (
  taskkill /PID %%P /T /F >nul 2>&1
)

rem 4. Stop Antigravity proxy
if exist "%HERE%antigravity-proxy\ag-proxy.pid" (
  for /f "usebackq" %%P in ("%HERE%antigravity-proxy\ag-proxy.pid") do taskkill /PID %%P /T /F >nul 2>&1
  del "%HERE%antigravity-proxy\ag-proxy.pid" >nul 2>&1
)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":43810 " ^| findstr "LISTENING"') do taskkill /PID %%P /T /F >nul 2>&1

ping -n 2 127.0.0.1 >nul
if exist "%STOPFILE%" del "%STOPFILE%" >nul 2>&1

echo [OK] Stopped WorkBuddy + Trae and Antigravity proxies.
goto :eof

:do_status
call :check_port_listening %PORT%
if "%PORT_LISTENING%"=="1" (
  echo [STATUS] WorkBuddy + Trae proxy is RUNNING on port %PORT%.
  exit /b 0
) else (
  echo [STATUS] WorkBuddy + Trae proxy is NOT RUNNING on port %PORT%.
  exit /b 1
)

:do_start
set "STOPFILE=%HERE%wt-proxy-%PORT%.stop"
set "PIDFILE=%HERE%wt-proxy-%PORT%.pid"
set "LOGFILE=%HERE%wt-proxy-%PORT%.out.log"
set "ERRFILE=%HERE%wt-proxy-%PORT%.err.log"

call :check_port_listening %PORT%
if "%PORT_LISTENING%"=="1" (
  echo [INFO] WorkBuddy + Trae proxy is already listening on port %PORT%.
  goto :start_done
)

call :find_python
if not defined PYEXE goto :no_python

echo ===========================================================
echo   Starting WorkBuddy + Trae proxy on port %PORT% ...
echo   Mode: Background with Auto-Healing Guardian
echo ===========================================================
echo.

if not "%TRAE_IDE_TOKEN%"=="" (
  echo   Trae failover: enabled
)

if exist "%STOPFILE%" del "%STOPFILE%" >nul 2>&1
if exist "%PIDFILE%" del "%PIDFILE%" >nul 2>&1

powershell -NoProfile -Command "$p = Start-Process -WindowStyle Hidden -FilePath cmd.exe -ArgumentList '/c','\"%~f0\" --worker %PORT%' -WorkingDirectory '%HERE%' -PassThru; [System.IO.File]::WriteAllText('%PIDFILE%', $p.Id)"

echo Waiting for proxy to be ready on port %PORT% ...
powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) { if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 }; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 goto :start_timeout

rem Check and start Antigravity proxy on 43810 if not running
call :check_port_listening 43810
if "%PORT_LISTENING%"=="0" if exist "%HERE%antigravity-proxy\package.json" (
  echo Starting Antigravity proxy on port 43810 ...
  powershell -NoProfile -Command "$ag = '%HERE%antigravity-proxy'; $p = Start-Process -WindowStyle Hidden -FilePath cmd.exe -ArgumentList '/c','npm start' -WorkingDirectory $ag -RedirectStandardOutput \"$ag\ag-proxy.log\" -RedirectStandardError \"$ag\ag-proxy.err.log\" -PassThru; [System.IO.File]::WriteAllText(\"$ag\ag-proxy.pid\", $p.Id)"
  powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) { if (Get-NetTCPConnection -LocalPort 43810 -State Listen -ErrorAction SilentlyContinue) { exit 0 }; Start-Sleep -Seconds 1 }; exit 1" >nul 2>&1
)

:start_done
echo.
echo ===========================================================
echo   [OK] WorkBuddy + Trae proxy is ACTIVE on port %PORT%
echo   [OK] Antigravity proxy is ACTIVE on port 43810
echo.
echo   Combined Dashboard : http://127.0.0.1:%PORT%/
echo   WorkBuddy API      : http://127.0.0.1:%PORT%/v1
echo   Antigravity API    : http://127.0.0.1:43810/v1
echo.
echo   Guardian is protecting this service in background.
echo ===========================================================
echo.
start http://127.0.0.1:%PORT%/
ping -n 4 127.0.0.1 >nul
exit /b 0

:interactive_menu
cls
echo ===========================================================
echo   WorkBuddy + Trae Proxy is ALREADY RUNNING on port %PORT%
echo.
echo   API       : http://127.0.0.1:%PORT%/v1
echo   Dashboard : http://127.0.0.1:%PORT%/
echo ===========================================================
echo.
echo   [1] Open Dashboard in browser
echo   [2] View latest logs
echo   [3] Restart service
echo   [4] Stop service (kill guardian and proxy)
echo   [5] Exit menu
echo.
set "CHOICE="
set /p "CHOICE=Enter choice [1-5] (default 1): "
if "%CHOICE%"=="" set "CHOICE=1"
if "%CHOICE%"=="1" (
  start http://127.0.0.1:%PORT%/
  exit /b 0
)
if "%CHOICE%"=="2" goto :show_logs
if "%CHOICE%"=="3" goto :do_restart
if "%CHOICE%"=="4" goto :do_stop
if "%CHOICE%"=="5" exit /b 0
goto :interactive_menu

:show_logs
set "LOGFILE=%HERE%wt-proxy-%PORT%.out.log"
set "ERRFILE=%HERE%wt-proxy-%PORT%.err.log"
echo.
echo --- Output Log (last 20 lines) ---
powershell -NoProfile -Command "if (Test-Path '%LOGFILE%') { Get-Content '%LOGFILE%' -Tail 20 } else { 'No log output yet.' }"
echo.
echo --- Error Log (last 10 lines) ---
powershell -NoProfile -Command "if (Test-Path '%ERRFILE%') { Get-Content '%ERRFILE%' -Tail 10 } else { 'No error log.' }"
echo.
pause
goto :interactive_menu

rem ===========================================================
rem  Helper Routines
rem ===========================================================

:check_port_listening
set "PORT_LISTENING=0"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%~1 " ^| findstr "LISTENING"') do set "PORT_LISTENING=1"
if "%PORT_LISTENING%"=="0" for /f "tokens=5" %%P in ('netstat -ano ^| findstr "127.0.0.1:%~1 " ^| findstr "LISTENING"') do set "PORT_LISTENING=1"
goto :eof

:find_python
set "PYEXE="

rem 1) bundled runtime
if exist "%HERE%python\python.exe" (
  "%HERE%python\python.exe" --version >nul 2>nul
  if not errorlevel 1 (
    set "PYEXE=%HERE%python\python.exe"
    goto :eof
  )
)

rem 2) python on PATH
python --version >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=python"
  goto :eof
)

rem 3) py launcher
py --version >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=py"
  goto :eof
)

rem 4) specific installed Python
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  goto :eof
)

rem 5) Codex runtimes
call :find_codex
goto :eof

:find_codex
if not exist "%USERPROFILE%\.cache\codex-runtimes" goto :eof
for /d %%D in ("%USERPROFILE%\.cache\codex-runtimes\*") do call :probe "%%~D"
goto :eof

:probe
if defined PYEXE goto :eof
for /f "delims=" %%P in ('dir /b /s "%~1\python.exe" 2^>nul') do call :probe_one "%%~P"
goto :eof

:probe_one
if defined PYEXE goto :eof
"%~1" --version >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
goto :eof

:no_script
echo [ERROR] wb_proxy.py not found next to this script.
echo         expected: %SCRIPT%
pause
exit /b 1

:no_python
echo [ERROR] No usable Python found.
pause
exit /b 1

:start_timeout
echo [ERROR] Proxy did not become ready on port %PORT% within 30 seconds.
echo Check log: %ERRFILE%
pause
exit /b 1
