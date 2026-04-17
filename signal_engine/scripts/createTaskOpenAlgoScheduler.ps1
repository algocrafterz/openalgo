# -------------------------------------------------------
# Create Windows Task Scheduler tasks for OpenAlgo
#
# Creates three tasks under the Anand user account:
#   1. openAlgoAutoStart  -- Weekdays 8:50 AM  -- long-running foreground launcher
#   2. openAlgoAutoStop   -- Weekdays 3:30 PM  -- graceful shutdown
#   3. openAlgoWatchdog   -- Weekdays 9:00 AM-3:25 PM, every 5 min -- crash recovery
#
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File createTaskOpenAlgoScheduler.ps1
#
# To customize times, edit the variables below.
# -------------------------------------------------------

$ps1Path = "$PSScriptRoot\openalgoctl.ps1"

if (!(Test-Path $ps1Path)) {
    Write-Host "ERROR: $ps1Path not found" -ForegroundColor Red
    exit 1
}

# --- Configuration ---
$startTime    = "8:50AM"
$stopTime     = "3:30PM"
$watchdogTime = "9:00AM"
$days         = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

# --- Principal: run as Anand in the interactive session ---
$principal = New-ScheduledTaskPrincipal `
    -UserId "Anand" `
    -LogonType Interactive `
    -RunLevel Limited

# -------------------------------------------------------
# Task 1: Auto Start
# Launches openalgoctl.ps1 run -- starts app.py + signal engine, then
# BLOCKS until either process exits or AutoStop kills them at 3:30 PM.
# StartWhenAvailable: fires at next login if machine was off at 8:50.
# -------------------------------------------------------

Unregister-ScheduledTask -TaskName "openAlgoAutoStart" -Confirm:$false -ErrorAction SilentlyContinue

$startAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1Path`" run"

$startTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $startTime

$startSettings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)

Register-ScheduledTask `
    -TaskName "openAlgoAutoStart" -TaskPath "\" `
    -Action $startAction -Trigger $startTrigger -Settings $startSettings -Principal $principal `
    -Force | Out-Null

Write-Host "Task 1 created: openAlgoAutoStart  (Weekdays $startTime -- long-running)" -ForegroundColor Green

# -------------------------------------------------------
# Task 2: Auto Stop
# Calls openalgoctl.ps1 stop -- sends shutdown Telegram notification,
# kills app.py + signal engine, terminates the AutoStart task window.
# -------------------------------------------------------

Unregister-ScheduledTask -TaskName "openAlgoAutoStop" -Confirm:$false -ErrorAction SilentlyContinue

$stopAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1Path`" stop"

$stopTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $stopTime

$stopSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName "openAlgoAutoStop" -TaskPath "\" `
    -Action $stopAction -Trigger $stopTrigger -Settings $stopSettings -Principal $principal `
    -Force | Out-Null

Write-Host "Task 2 created: openAlgoAutoStop   (Weekdays $stopTime)" -ForegroundColor Green

# -------------------------------------------------------
# Task 3: Watchdog (XML-based -- only reliable way to combine
# weekly day filter with sub-hourly repetition in PowerShell)
#
# Calls openalgoctl.ps1 start (idempotent):
#   - Services running  -> health check passes -> exits silently (no-op)
#   - Services crashed  -> relaunches in a new hidden window
# Maximum downtime before auto-recovery: 5 minutes.
# -------------------------------------------------------

Unregister-ScheduledTask -TaskName "openAlgoWatchdog" -Confirm:$false -ErrorAction SilentlyContinue

$watchdogXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>OpenAlgo watchdog - restarts services if crashed. Fires every 5 min on weekdays 09:00-15:25.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <Duration>PT6H25M</Duration>
        <StopAtDurationEnd>true</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-05T09:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Monday />
          <Tuesday />
          <Wednesday />
          <Thursday />
          <Friday />
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>Anand</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <ExecutionTimeLimit>PT3M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "$ps1Path" start</Arguments>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask `
    -TaskName "openAlgoWatchdog" -TaskPath "\" `
    -Xml $watchdogXml `
    -Force | Out-Null

Write-Host "Task 3 created: openAlgoWatchdog   (Weekdays $watchdogTime-3:25PM, every 5 min)" -ForegroundColor Green

# --- Summary ---
Write-Host ""
Write-Host "All 3 tasks registered under user: Anand" -ForegroundColor Cyan
Write-Host ""
Write-Host "How they work together:" -ForegroundColor Cyan
Write-Host "  8:50 AM  openAlgoAutoStart -- starts app.py + signal engine, stays running all day" -ForegroundColor White
Write-Host "  9:00 AM  openAlgoWatchdog  -- fires every 5 min; no-op if running, restarts if dead" -ForegroundColor White
Write-Host "  3:30 PM  openAlgoAutoStop  -- sends shutdown notification, kills all services" -ForegroundColor White
Write-Host ""
Write-Host "Script path: $ps1Path" -ForegroundColor Cyan
