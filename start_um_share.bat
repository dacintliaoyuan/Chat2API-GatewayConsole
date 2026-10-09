@echo off
setlocal
python "%~dp0chat2api\share_gateway.py" %*
exit /b %errorlevel%
