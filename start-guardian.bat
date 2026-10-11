@echo off
setlocal
cd /d "%~dp0"
title WorkBuddy-Trae-Proxy-Guardian

:loop
echo [%date% %time%] Starting wb_proxy... >> proxy-guardian.log
py wb_proxy.py --port 28087 2>> wb-proxy.err.log
set EXIT_CODE=%ERRORLEVEL%
echo [%date% %time%] Process exited with code %EXIT_CODE% >> proxy-guardian.log
echo [WARN] Proxy exited at %date% %time% with code %EXIT_CODE%. Restarting in 3s...
ping -n 4 127.0.0.1 >nul
goto loop
