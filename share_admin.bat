@echo off
rem share_admin.bat — forward every argument to the share management CLI.
setlocal
python "%~dp0share_admin.py" %*
exit /b %errorlevel%
