@echo off
rem Rollback (#85): set the usage StartPath back to start_usage_service.bat in
rem lib\gateway-runtime.ps1. This file keeps the Python 8001 command; no
rem reinstall and no port change are needed.
cd /d "%~dp0"
python usagesrv.py
