# -------------------------------------------------------
# Create Windows Task Scheduler task for OpenAlgo AutoStart
# Run as Administrator: powershell -ExecutionPolicy Bypass -File createTaskOpenAlgoAutoStart.ps1
# -------------------------------------------------------

# Delete existing task
Unregister-ScheduledTask `
    -TaskName "openAlgoAutoStart" `
    -Confirm:$false `
    -ErrorAction SilentlyContinue

$ps1Path = "$PSScriptRoot\openAlgoAutoStart.ps1"

if (!(Test-Path $ps1Path)) {
    Write-Host "ERROR: $ps1Path not found" -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1Path`""

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At 8:50AM

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName "openAlgoAutoStart" `
    -TaskPath "\" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force

Write-Host "Task created: openAlgoAutoStart (Weekdays 8:50 AM)" -ForegroundColor Green
Write-Host "Script path: $ps1Path" -ForegroundColor Cyan
