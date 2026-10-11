@echo off
rem Delegate to all-in-one launcher
call "%~dp0wt-proxy-start.bat" status %*
timeout /t 5 >nul
