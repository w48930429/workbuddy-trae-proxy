@echo off
setlocal
rem ===========================================================
rem  WorkBuddy + Trae proxy - LEGACY standalone guardian loop
rem
rem  WHY THIS FILE EXISTS
rem    wb_proxy.py registers a Windows console handler and exits on
rem    CTRL_CLOSE_EVENT / CTRL_LOGOFF_EVENT / CTRL_SHUTDOWN_EVENT
rem    (see install_console_close_handler). Closing the console
rem    window therefore STOPS the server with no error log. This loop
rem    restarts it 3 seconds after any exit and records the exit code,
rem    so "it died by itself" becomes diagnosable.
rem
rem  PREFER THE ALL-IN-ONE LAUNCHER
rem    wt-proxy-start.bat            (start with guardian, default port)
rem    wt-proxy-start.bat stop       (stop guardian + proxy + antigravity)
rem    wt-proxy-start.bat status
rem    wt-proxy-stop.bat             (delegates to the line above)
rem    Use that path unless you specifically want this minimal loop:
rem    killing the python process alone is NOT enough here, the loop
rem    restarts it. Stop this loop by closing this window or by
rem    killing the cmd.exe process that runs this file.
rem
rem  LOGS
rem    proxy-guardian.log : start time, exit code, restart events
rem    wb-proxy.err.log   : the server's own stderr
rem
rem  ASCII-only on purpose: .bat files are parsed with the console
rem  code page, so non-ASCII text breaks the parser.
rem ===========================================================

:loop
echo [%date% %time%] Starting wb_proxy... >> proxy-guardian.log
py wb_proxy.py --port 28087 2>> wb-proxy.err.log
set EXIT_CODE=%ERRORLEVEL%
rem Exit code 0 usually means the console window/logoff event fired.
rem Non-zero points at a real failure; the full traceback lands in wb-proxy.err.log.
echo [%date% %time%] Process exited with code %EXIT_CODE% >> proxy-guardian.log
echo [WARN] Proxy exited at %date% %time% with code %EXIT_CODE%. Restarting in 3s...
ping -n 4 127.0.0.1 >nul
goto loop
