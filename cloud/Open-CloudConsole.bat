@echo off
setlocal
title UM Cloud Console (SSH tunnel)
cd /d "%~dp0"
rem The SSH tunnel goes over Tailscale, not an HTTP proxy. Clear proxy
rem variables so the loopback health check never leaves through a proxy.
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "NO_PROXY=*"
set "no_proxy=*"
rem The desktop entry opens both the cloud console and the private login window.
set "UM_CLOUD_CONSOLE_OPEN_LOGIN=1"
rem PowerShell 7 is the project default; fall back to Windows PowerShell
rem only when pwsh is not installed on this machine.
where pwsh >nul 2>nul
if errorlevel 1 goto legacy
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0Open-CloudConsole.ps1" %*
exit /b %ERRORLEVEL%
:legacy
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Open-CloudConsole.ps1" %*
exit /b %ERRORLEVEL%
