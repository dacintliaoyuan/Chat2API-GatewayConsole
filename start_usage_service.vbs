' Hidden adapter for the usage aggregation service.
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & root & "\invoke-gateway-service.ps1"" -Key usage -Action Start"
CreateObject("Wscript.Shell").Run command, 0, False
