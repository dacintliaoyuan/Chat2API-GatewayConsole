' Hidden launcher for lib\watch-share-backend.ps1 (dedicated 8110 backend
' watch, scheduled every minute). powershell -WindowStyle Hidden still flashes
' a console for a moment; run 0 never creates a visible window. Waits for the
' watch and hands its exit code to Task Scheduler, so the task's Last Run
' Result is the watch's own result (0 no action / 1 refused / 2 unconfirmed).
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & root & "\watch-share-backend.ps1"""
WScript.Quit CreateObject("WScript.Shell").Run(command, 0, True)
