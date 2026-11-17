@echo off
rem Rollback (#80): set the chat2api StartPath back to start_chat2api.bat in
rem lib\gateway-runtime.ps1. This file keeps the Python 8000 command; no
rem reinstall and no port change are needed.
rem Direct connection: clear proxy env vars and set NO_PROXY=* (no VPN/system proxy).
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "NO_PROXY=*"
set "no_proxy=*"
rem Browser-origin gate (chat2api.py:279-288): a POST that CARRIES an Origin
rem header is rejected unless that exact origin is listed here. KISS Translator
rem runs its requests from the extension service worker, so the browser stamps
rem them with the extension's own chrome-extension:// origin; the local gateway
rem answered 403 origin_denied (2026-10-14, KISS test button). One id covers
rem Chrome and Edge: a Web Store id is derived from the signing key.
set "CHAT2API_ALLOWED_ORIGINS=chrome-extension://bdiifdefkgmcblbcghdlonllpjhhjgof"
title chat2api service
rem Launcher for openwebui-chat2api (Open WebUI -> local OpenAI-compatible API)
rem Foreground window: logs visible here; closing this window stops the service.
cd /d "%~dp0chat2api"
if defined SELFUSEAI_HIDDEN_LAUNCH goto hidden
echo Starting chat2api on http://127.0.0.1:8000 ...
rem 2026-09-11: UM disables API keys (enable_api_keys=false), --use-api-key removed.
rem The --effort-model GLM-5.2-NVFP4 base was retired from the UM catalog; drop the flag.
python chat2api.py --base-url https://chat.genai.um.edu.mo
echo.
echo chat2api exited.
pause
exit /b

:hidden
rem Hidden launch (lib\gateway-runtime.ps1 HiddenBatch, 2026-09-30): nobody can
rem press a key in an invisible window, so instead of pausing forever this run's
rem console output goes to chat2api.console.log (the previous run is kept as
rem chat2api.console.prev.log) and the launcher records how Python ended. When
rem the whole process tree is killed from outside, the "python exited" line is
rem missing - that absence is itself the evidence.
rem Keep this python command identical to the visible branch above.
if exist chat2api.console.log move /y chat2api.console.log chat2api.console.prev.log >nul
echo [launcher] %DATE% %TIME% starting hidden >> chat2api.console.log
python chat2api.py --base-url https://chat.genai.um.edu.mo >> chat2api.console.log 2>&1
echo [launcher] %DATE% %TIME% python exited rc=%ERRORLEVEL% >> chat2api.console.log
exit /b
