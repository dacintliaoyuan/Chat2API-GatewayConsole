' Hidden launcher for lib\watch-chat2api.ps1 (scheduled every minute).
' powershell -WindowStyle Hidden still flashes a console for a moment; run 0
' never creates a visible window. Waits for the watch and hands its exit code
' to Task Scheduler, so the task's Last Run Result is the watch's own result.
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & root & "\watch-chat2api.ps1"""
WScript.Quit CreateObject("WScript.Shell").Run(command, 0, True)
