@echo off
rem run-broker.cmd - SYSTEM scheduled task wrapper for the broker host
rem (D4 prerequisite). H3 validates the PYTHONHOME chain under SYSTEM:
rem the interpreter runs from the REAL Python314 install (um_share RX kept),
rem bytecode writing is disabled and PYTHONPATH is cleared so nothing is
rem imported from an ordinary-user-writable path (D0 27.4). The module
rem closure under C:\ProgramData\UMShareRun is copied+hash-verified by
rem install-share-broker.ps1; extra arguments are appended verbatim.
setlocal
set "PYTHONHOME=C:\Users\dacint\AppData\Local\Programs\Python\Python314"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONPATH="
"C:\ProgramData\UMShareRun\um-share-python.exe" "C:\ProgramData\UMShareRun\share_broker_server.py" --broker-dir "C:\ProgramData\UMShareRun\broker" --key-file "C:\ProgramData\UMShareRun\broker\broker-control-key" --instance-key-file "C:\ProgramData\UMShareRun\instance-channel-key" --guard-status "C:\ProgramData\UMShareRun\guard-status.json" --projection-path "C:\ProgramData\UMShareRun\um-share-projection.json" --share-task "umshare-live" %*
endlocal
