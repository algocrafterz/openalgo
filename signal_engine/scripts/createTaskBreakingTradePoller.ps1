# -------------------------------------------------------
# Create Windows Task Scheduler tasks for the BreakingTrade scanner poller.
#
# Creates three tasks under the Anand user account:
#   1. breakingTradeAutoStart  -- Weekdays 9:10 AM -- starts the poller
#   2. breakingTradeWatchdog   -- Weekdays 9:15 AM-3:15 PM, every 5 min -- restarts if dead
#   3. breakingTradeAutoStop   -- Weekdays 3:35 PM -- stops the poller after the close
#
# WHY A WATCHDOG AND NOT JUST AN AUTOSTART
# On 2026-09-04 the poller died mid-session and 25 of 27 scheduled polls were lost.
# A start-only task would not have caught that. `breakingtradectl.ps1 start` is
# idempotent - it refuses to launch a second copy - so a five-minute watchdog is
# safe, and it bounds the worst-case loss to a single poll.
#
# Intraday scanner data CANNOT be back-filled (the vendor serves one snapshot per
# day, at the close), so a lost poll is lost permanently. That is what justifies
# the redundancy.
#
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File createTaskBreakingTradePoller.ps1
# -------------------------------------------------------

$ps1Path = "$PSScriptRoot\breakingtradectl.ps1"

if (!(Test-Path $ps1Path)) {
    Write-Host "ERROR: $ps1Path not found" -ForegroundColor Red
    exit 1
}

# --- Configuration ---
# 9:10 gives the machine time to wake and WSL to come up before the 9:20 first poll.
$startTime = "9:10AM"
# 3:35 PM: after the 15:15 continuous-trading cutoff and the last scheduled poll.
$stopTime  = "3:35PM"
$days      = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

# --- Principal: run as Anand in the interactive session (same as the openAlgo tasks) ---
$principal = New-ScheduledTaskPrincipal `
    -UserId "Anand" `
    -LogonType Interactive `
    -RunLevel Limited

# -------------------------------------------------------
# Task 1: Auto Start
# WakeToRun matches the OpenAlgo tasks: the laptop wakes on mains power and starts
# collecting without anyone being present.
# -------------------------------------------------------

Unregister-ScheduledTask -TaskName "breakingTradeAutoStart" -Confirm:$false -ErrorAction SilentlyContinue

$startAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1Path`" start"

$startTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $startTime

$startSettings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName "breakingTradeAutoStart" -TaskPath "\" `
    -Action $startAction -Trigger $startTrigger -Settings $startSettings -Principal $principal `
    -Force | Out-Null

Write-Host "Task 1 created: breakingTradeAutoStart  (Weekdays $startTime)" -ForegroundColor Green

# -------------------------------------------------------
# Task 2: Watchdog
# XML-based, because combining a weekly day filter with sub-hourly repetition is
# only reliable that way in PowerShell (same approach as openAlgoWatchdog).
# Runs 09:15-15:15, every 5 minutes.
# -------------------------------------------------------

Unregister-ScheduledTask -TaskName "breakingTradeWatchdog" -Confirm:$false -ErrorAction SilentlyContinue

$watchdogXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>BreakingTrade poller watchdog - restarts the scanner poller if it died. Every 5 min, weekdays 09:15-15:15. Intraday scanner data cannot be back-filled, so a lost poll is lost permanently.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <Duration>PT6H</Duration>
        <StopAtDurationEnd>true</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-05T09:15:00</StartBoundary>
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
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
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

Register-ScheduledTask -TaskName "breakingTradeWatchdog" -TaskPath "\" -Xml $watchdogXml -Force | Out-Null

Write-Host "Task 2 created: breakingTradeWatchdog  (Weekdays 09:15-15:15, every 5 min)" -ForegroundColor Green

# -------------------------------------------------------
# Task 3: Auto Stop
# Releases the browser and its memory once the last scheduled poll is done.
# -------------------------------------------------------

Unregister-ScheduledTask -TaskName "breakingTradeAutoStop" -Confirm:$false -ErrorAction SilentlyContinue

$stopAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1Path`" stop"

$stopTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $stopTime

$stopSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName "breakingTradeAutoStop" -TaskPath "\" `
    -Action $stopAction -Trigger $stopTrigger -Settings $stopSettings -Principal $principal `
    -Force | Out-Null

Write-Host "Task 3 created: breakingTradeAutoStop  (Weekdays $stopTime)" -ForegroundColor Green

Write-Host ""
Write-Host "Done. Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName 'breakingTrade*' | Format-Table TaskName, State"
Write-Host "  .\breakingtradectl.ps1 status"
Write-Host "  .\breakingtradectl.ps1 audit      # after a session, confirms nothing was missed"
