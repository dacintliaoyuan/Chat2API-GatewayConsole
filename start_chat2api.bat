@echo off
setlocal
cd /d "%~dp0" || exit /b 1
title chat2api service
if defined AIGATEWAY_PUBLIC_HIDDEN_LAUNCH goto hidden
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-public-service.ps1" -Key chat2api
set "PUBLIC_SERVICE_EXIT=%ERRORLEVEL%"
echo.
echo chat2api exited.
pause
exit /b %PUBLIC_SERVICE_EXIT%
:hidden
cd /d "%~dp0chat2api" || exit /b 1
if exist chat2api.console.log move /y chat2api.console.log chat2api.console.prev.log >nul
echo [launcher] %DATE% %TIME% starting hidden >> chat2api.console.log
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-public-service.ps1" -Key chat2api >> chat2api.console.log 2>&1
set "PUBLIC_SERVICE_EXIT=%ERRORLEVEL%"
echo [launcher] %DATE% %TIME% python exited rc=%PUBLIC_SERVICE_EXIT% >> chat2api.console.log
exit /b %PUBLIC_SERVICE_EXIT%
