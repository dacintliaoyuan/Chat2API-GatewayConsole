@echo off
rem 费用统计聚合服务启动器（usagesrv.py, 127.0.0.1:8001），由 start_usage_service.vbs 隐藏调用
cd /d "%~dp0"
python usagesrv.py
