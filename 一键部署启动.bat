@echo off
setlocal
chcp 65001 >nul
title Chat2API - Setup and Start
cd /d "%~dp0" || exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-and-start.ps1" %*
set "PUBLIC_SETUP_EXIT=%ERRORLEVEL%"
echo.
if not "%PUBLIC_SETUP_EXIT%"=="0" echo Setup/start failed. See the message above.
pause
exit /b %PUBLIC_SETUP_EXIT%
