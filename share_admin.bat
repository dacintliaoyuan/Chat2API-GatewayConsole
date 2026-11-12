@echo off
rem share_admin.bat — forward every argument to the share management CLI.
setlocal
python "%~dp0share\admin.py" %*
exit /b %errorlevel%
