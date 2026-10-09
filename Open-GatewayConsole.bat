@echo off
setlocal
title AI Gateway Console (web)
cd /d "%~dp0"
rem Direct connection: clear proxy env vars and set NO_PROXY=* (no VPN/system proxy).
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "NO_PROXY=*"
set "no_proxy=*"
rem Delegate to the web-console entry; service identity, ports and the
rem start strategy are owned by lib\gateway-runtime.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Open-GatewayConsole.ps1"
endlocal
