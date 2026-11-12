@echo off
setlocal
title AI Gateway Console Prototype
cd /d "%~dp0"
rem Same proxy hygiene as the production entry: talk to 127.0.0.1 directly.
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "NO_PROXY=*"
set "no_proxy=*"
rem Prototype dev server (vite, 127.0.0.1:5173): ensure it is running, then
rem open the console in the default browser. Chinese guidance lives in the
rem PowerShell helper (batch files here must stay ASCII for the GBK console).
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure-prototype-server.ps1"
if errorlevel 1 (
  echo.
  echo Prototype startup failed - see messages above or tmp\console-dev.log
  pause
  exit /b 1
)
start "" "http://127.0.0.1:5173/"
endlocal
