@echo off
setlocal
cd /d "%~dp0"
python check_port_bindings.py %*
endlocal
