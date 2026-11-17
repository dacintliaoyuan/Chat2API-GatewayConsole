@echo off
setlocal
title 一键部署本机网关
cd /d "%~dp0"
rem 直连本机：清掉代理环境变量，避免健康检查走代理。
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "NO_PROXY=*"
set "no_proxy=*"
rem PowerShell 7 是项目默认；未安装时回退到 Windows PowerShell。
where pwsh >nul 2>nul
if errorlevel 1 goto legacy
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy-local-gateway.ps1" %*
exit /b %ERRORLEVEL%
:legacy
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy-local-gateway.ps1" %*
exit /b %ERRORLEVEL%
