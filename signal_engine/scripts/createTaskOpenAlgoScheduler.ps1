# -------------------------------------------------------
# Create Windows Task Scheduler tasks for OpenAlgo
#
# Creates four tasks under the Anand user account:
#   1. openAlgoAutoStart  -- Weekdays 8:50 AM  -- long-running foreground launcher
#   2. openAlgoAutoStop   -- Weekdays 3:30 PM  -- graceful shutdown
#   3. openAlgoWatchdog   -- Weekdays 9:00 AM-3:25 PM, every 5 min -- crash recovery
#   4. openAlgoSquareOff  -- Weekdays 3:02 PM  -- failsafe MIS position close
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
$startTime      = "8:50AM"
$stopTime       = "4:00PM"
$squareoffTime  = "2:55PM"
$watchdogTime   = "9:00AM"
$days           = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

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
    <Description>OpenAlgo watchdog - restarts services if crashed. Fires every 5 min on weekdays 09:00-16:00.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <Duration>PT7H</Duration>
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
    <!-- Was PT3M. A normal boot (network wait + NTP-sync wait for WSL2 clock
         drift + broker login) can legitimately run past 3 minutes; hard-killing
         it mid-boot just to retry 5 minutes later produced the exact
         kill/relaunch churn this was meant to prevent. MultipleInstancesPolicy
         below already stops overlapping watchdog runs, so a generous limit
         here only guards against a truly wedged wsl.exe/powershell.exe, not
         against pile-up. -->
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
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

Write-Host "Task 3 created: openAlgoWatchdog   (Weekdays $watchdogTime-4:00PM, every 5 min)" -ForegroundColor Green

# -------------------------------------------------------
# Task 4: Square-Off (3:02 PM failsafe)
# Calls openalgoctl.ps1 squareoff — cancels all pending orders and closes all
# MIS positions via the OpenAlgo API.
#
# Fires 2 minutes after the signal engine's own 3:00 PM time exit:
#   - If engine closed positions successfully → this is a no-op (nothing to close)
#   - If engine is crashed, frozen, or system was asleep → this saves the day
#
# WakeToRun: true — wakes the machine from sleep to fire at 3:02 PM.
# This is the only task with WakeToRun at close time, making it the
# hard guarantee that positions are closed even on a sleeping laptop.
# -------------------------------------------------------

Unregister-ScheduledTask -TaskName "openAlgoSquareOff" -Confirm:$false -ErrorAction SilentlyContinue

$squareoffAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1Path`" squareoff"

$squareoffTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $squareoffTime

$squareoffSettings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName "openAlgoSquareOff" -TaskPath "\" `
    -Action $squareoffAction -Trigger $squareoffTrigger -Settings $squareoffSettings -Principal $principal `
    -Force | Out-Null

Write-Host "Task 4 created: openAlgoSquareOff  (Weekdays $squareoffTime -- failsafe MIS close, WakeToRun)" -ForegroundColor Green

# -------------------------------------------------------
# Task 5: Heartbeat Check (dead-man's switch)
# Runs heartbeat_check.ps1 every 10 min, 9:05 AM-3:25 PM weekdays. Has NO
# dependency on WSL/bash/Python -- catches the case where the rest of this
# stack fails before it can alert on itself (see heartbeat_check.ps1 header;
# this is what would have caught the 2026-09-15/16 silent outage).
# -------------------------------------------------------

Unregister-ScheduledTask -TaskName "openAlgoHeartbeatCheck" -Confirm:$false -ErrorAction SilentlyContinue

$heartbeatXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>OpenAlgo heartbeat check -- alerts if the stack goes silent during market hours. Independent of WSL/bash/Python.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition>
        <Interval>PT10M</Interval>
        <Duration>PT6H55M</Duration>
        <StopAtDurationEnd>true</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-05T09:05:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Monday /><Tuesday /><Wednesday /><Thursday /><Friday />
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
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "$PSScriptRoot\heartbeat_check.ps1"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask `
    -TaskName "openAlgoHeartbeatCheck" -TaskPath "\" `
    -Xml $heartbeatXml `
    -Force | Out-Null

Write-Host "Task 5 created: openAlgoHeartbeatCheck (Weekdays 9:05AM-4:00PM, every 10 min -- dead-man's switch)" -ForegroundColor Green

# --- Summary ---
Write-Host ""
Write-Host "All 5 tasks registered under user: Anand" -ForegroundColor Cyan
Write-Host ""
Write-Host "How they work together:" -ForegroundColor Cyan
Write-Host "  8:50 AM  openAlgoAutoStart      -- starts app.py + signal engine, stays running all day" -ForegroundColor White
Write-Host "  9:00 AM  openAlgoWatchdog       -- fires every 5 min; no-op if running, restarts if dead" -ForegroundColor White
Write-Host "  9:05 AM  openAlgoHeartbeatCheck -- fires every 10 min; dead-man's switch, independent of WSL/bash/Python" -ForegroundColor White
Write-Host "  2:55 PM  openAlgoSquareOff      -- failsafe: closes MIS positions if engine didn't (WakeToRun)" -ForegroundColor White
Write-Host "  4:00 PM  openAlgoAutoStop       -- sends shutdown notification, kills all services (later than market close for the momentum-rank EOD scan)" -ForegroundColor White
Write-Host ""
Write-Host "Square-off design:" -ForegroundColor Cyan
Write-Host "  Signal engine fires its own exit at 3:00 PM (asyncio scheduler, 5s polling)" -ForegroundColor White
Write-Host "  openAlgoSquareOff now fires at 2:55 PM -- BEFORE the engine's own 3:00 PM exit, not after." -ForegroundColor Yellow
Write-Host "  This means it no longer waits to see whether the engine would have closed the position" -ForegroundColor Yellow
Write-Host "  itself; it force-closes all MIS positions every day at 2:55 regardless. Confirm this is intended." -ForegroundColor Yellow
Write-Host ""
Write-Host "Script path: $ps1Path" -ForegroundColor Cyan
