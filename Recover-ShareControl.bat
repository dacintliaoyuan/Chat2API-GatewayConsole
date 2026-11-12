@echo off
setlocal
title Share Control Recovery
cd /d "%~dp0"
set "SHARE_RECOVERY_PS=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not exist "%SHARE_RECOVERY_PS%" (
    echo PowerShell 7 is required. Install it before running this recovery entry.
    pause
    exit /b 1
)
"%SHARE_RECOVERY_PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Recover-ShareControl.ps1" -Action Recover
set "SHARE_RECOVERY_EXIT=%ERRORLEVEL%"
echo.
pause
endlocal & exit /b %SHARE_RECOVERY_EXIT%
