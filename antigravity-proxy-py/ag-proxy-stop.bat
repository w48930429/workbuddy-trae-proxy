@echo off
setlocal
set PORT=43811
set PIDFILE=%~dp0ag-proxy.pid

if exist "%PIDFILE%" for /f %%P in (%PIDFILE%) do (
  tasklist /FI "PID eq %%P" 2>nul | find "%%P" >nul && (taskkill /PID %%P /T /F >nul & echo Stopped PID %%P.)
)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  taskkill /PID %%P /T /F >nul 2>&1 && echo Killed port listener PID %%P.
)
del "%PIDFILE%" >nul 2>&1
echo Done.
timeout /t 3 >nul
