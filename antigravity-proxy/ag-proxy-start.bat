@echo off
setlocal
set PORT=43810
set PROJDIR=%~dp0
set PROJDIR=%PROJDIR:~0,-1%
set PIDFILE=%~dp0ag-proxy.pid
set LOGFILE=%~dp0ag-proxy.log
set ERRFILE=%~dp0ag-proxy.err.log

if exist "%PIDFILE%" for /f %%P in (%PIDFILE%) do (
  tasklist /FI "PID eq %%P" 2>nul | find "%%P" >nul && (echo Already running, PID %%P. & timeout /t 3 >nul & exit /b 0)
)
echo Starting antigravity-proxy on port %PORT% ...
powershell -NoProfile -Command "$p = Start-Process -WindowStyle Hidden -FilePath cmd.exe -ArgumentList '/c','npm start' -WorkingDirectory '%PROJDIR%' -RedirectStandardOutput '%LOGFILE%' -RedirectStandardError '%ERRFILE%' -PassThru; [System.IO.File]::WriteAllText('%PIDFILE%', $p.Id)"
if not exist "%PIDFILE%" (echo Failed to start. & pause & exit /b 1)
for /f %%P in (%PIDFILE%) do echo Launched background PID %%P. Waiting for port %PORT% ...
powershell -NoProfile -Command "for ($i=0; $i -lt 90; $i++) { if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 }; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 (
  echo Port not ready. Check log: %ERRFILE%
) else (
  echo Ready. Opening http://127.0.0.1:%PORT%/admin ...
  start http://127.0.0.1:%PORT%/admin
)
pause
exit /b 0