@echo off
setlocal
set PORT=43810
set PIDFILE=%~dp0ag-proxy.pid

set RUNNING=0
if exist "%PIDFILE%" for /f %%P in (%PIDFILE%) do (
  tasklist /FI "PID eq %%P" 2>nul | find "%%P" >nul && (echo Running. PID %%P & set RUNNING=1)
)
if "%RUNNING%"=="0" (
  netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul && (echo Port %PORT% is listening. & set RUNNING=1)
)
if "%RUNNING%"=="0" echo Not running.
echo Open: http://127.0.0.1:%PORT%/admin
timeout /t 5 >nul