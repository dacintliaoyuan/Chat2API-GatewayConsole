@echo off
setlocal
cd /d "%~dp0" || exit /b 1
if defined AIGATEWAY_PUBLIC_HIDDEN_LAUNCH goto hidden
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-public-service.ps1" -Key usage
exit /b %ERRORLEVEL%
:hidden
if exist usagesrv.console.log move /y usagesrv.console.log usagesrv.console.prev.log >nul
echo [launcher] %DATE% %TIME% starting hidden >> usagesrv.console.log
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-public-service.ps1" -Key usage >> usagesrv.console.log 2>&1
set "PUBLIC_SERVICE_EXIT=%ERRORLEVEL%"
echo [launcher] %DATE% %TIME% python exited rc=%PUBLIC_SERVICE_EXIT% >> usagesrv.console.log
exit /b %PUBLIC_SERVICE_EXIT%
