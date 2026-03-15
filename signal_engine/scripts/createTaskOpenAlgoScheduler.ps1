# -------------------------------------------------------
# Create Windows Task Scheduler tasks for OpenAlgo
#
# Creates two tasks:
#   1. openAlgoAutoStart  — Weekdays 8:50 AM (start services)
#   2. openAlgoAutoStop   — Weekdays 3:30 PM (stop services)
#
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File createTaskOpenAlgoScheduler.ps1
#
# To customize times, edit $startTime / $stopTime below.
# -------------------------------------------------------

$ps1Path = "$PSScriptRoot\openalgoctl.ps1"

if (!(Test-Path $ps1Path)) {
    Write-Host "ERROR: $ps1Path not found" -ForegroundColor Red
    exit 1
}

# --- Configuration ---
$startTime = "8:50AM"
$stopTime  = "3:30PM"
$days      = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

# --- Shared settings ---
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# --- Task 1: Auto Start ---

Unregister-ScheduledTask `
    -TaskName "openAlgoAutoStart" `
    -Confirm:$false `
    -ErrorAction SilentlyContinue

$startAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1Path`" run"

$startTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek $days `
    -At $startTime

$startSettings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)

Register-ScheduledTask `
    -TaskName "openAlgoAutoStart" `
    -TaskPath "\" `
    -Action $startAction `
    -Trigger $startTrigger `
    -Settings $startSettings `
    -Principal $principal `
    -Force

Write-Host "Task created: openAlgoAutoStart (Weekdays $startTime)" -ForegroundColor Green

# --- Task 2: Auto Stop ---

Unregister-ScheduledTask `
    -TaskName "openAlgoAutoStop" `
    -Confirm:$false `
    -ErrorAction SilentlyContinue

$stopAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1Path`" stop"

$stopTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek $days `
    -At $stopTime

$stopSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName "openAlgoAutoStop" `
    -TaskPath "\" `
    -Action $stopAction `
    -Trigger $stopTrigger `
    -Settings $stopSettings `
    -Principal $principal `
    -Force

Write-Host "Task created: openAlgoAutoStop  (Weekdays $stopTime)" -ForegroundColor Green

# --- Summary ---
Write-Host ""
Write-Host "Schedule:" -ForegroundColor Cyan
Write-Host "  Start: $startTime  ->  openalgoctl.ps1 run" -ForegroundColor Cyan
Write-Host "  Stop:  $stopTime  ->  openalgoctl.ps1 stop" -ForegroundColor Cyan
Write-Host "  Days:  $($days -join ', ')" -ForegroundColor Cyan
Write-Host ""
Write-Host "Script path: $ps1Path" -ForegroundColor Cyan
