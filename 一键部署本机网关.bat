@echo off
setlocal
title Chat2API Gateway Deploy
cd /d "%~dp0"
rem Direct connection: clear proxy env vars so the loopback health check
rem never leaves through a proxy.
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "NO_PROXY=*"
set "no_proxy=*"
rem PowerShell 7 is the project default; fall back to Windows PowerShell
rem only when pwsh is not installed on this machine.
where pwsh >nul 2>nul
if errorlevel 1 goto legacy
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy-local-gateway.ps1" %*
exit /b %ERRORLEVEL%
:legacy
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy-local-gateway.ps1" %*
exit /b %ERRORLEVEL%
