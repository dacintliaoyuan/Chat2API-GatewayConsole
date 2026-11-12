@echo off
setlocal
python "%~dp0chat2api\share_entry.py" %*
exit /b %errorlevel%
