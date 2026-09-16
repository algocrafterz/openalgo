# -------------------------------------------------------
# OpenAlgo Heartbeat Check (Windows, dependency-free)
#
# Purpose: catch the failure class that openalgoctl.ps1/.sh cannot alert on
# themselves -- a failure that happens BEFORE they can run at all (lost
# executable bit, WSL not reachable, a typo in a script, etc). On 2026-09-15
# openalgoctl.sh silently lost its +x bit; every scheduled start/watchdog
# attempt afterwards failed instantly with "Permission Denied" before a
# single log line or Telegram message could be sent. The stack was down for
# 3.5+ hours of a trading day with zero notification.
#
# This script is deliberately as simple as possible and has NO dependency on
# WSL executing correctly, on Python, or on the Telegram/Telethon session
# that the rest of the stack uses -- so it keeps working even when all of
# that is broken. It only reads a heartbeat timestamp file (written every
# ~5s by openalgoctl.sh's run loop while the stack is genuinely healthy) via
# the WSL UNC path, and raises a blocking, unmissable Windows alert if it's
# stale during market hours.
#
# Registered as its own Task Scheduler task (openAlgoHeartbeatCheck), firing
# every 10 minutes, 09:05-15:25 IST weekdays -- see createTaskOpenAlgoScheduler.ps1.
# -------------------------------------------------------

$heartbeatPath = "\\wsl.localhost\Ubuntu-24.04\home\anand\github\openalgo\signal_engine\logs\heartbeat.txt"
$alertedFlag   = "$env:TEMP\openalgo_heartbeat_alerted.flag"
$staleMinutes  = 10

$now = Get-Date
$isWeekday = $now.DayOfWeek -notin @("Saturday", "Sunday")
$marketOpen = $now.TimeOfDay -ge (New-TimeSpan -Hours 9 -Minutes 5)
$marketClose = $now.TimeOfDay -le (New-TimeSpan -Hours 15 -Minutes 25)

if (-not ($isWeekday -and $marketOpen -and $marketClose)) {
    # Outside the window the stack is expected to be up -- nothing to check.
    exit 0
}

$stale = $true
if (Test-Path $heartbeatPath) {
    $age = ($now - (Get-Item $heartbeatPath).LastWriteTime).TotalMinutes
    if ($age -lt $staleMinutes) {
        $stale = $false
    }
}

if (-not $stale) {
    # Healthy -- clear any previous alert so the next real outage re-alerts.
    Remove-Item $alertedFlag -Force -ErrorAction SilentlyContinue
    exit 0
}

# Already alerted for this ongoing outage -- don't spam a MessageBox every
# 10 minutes for the same unresolved problem.
if (Test-Path $alertedFlag) {
    exit 0
}

New-Item -Path $alertedFlag -ItemType File -Force | Out-Null

$msg = "OpenAlgo heartbeat is stale or missing during market hours.`n`n" +
       "The trading stack (app.py + signal engine) may be down and the " +
       "normal Telegram alerting path may itself be broken (this is exactly " +
       "the failure mode that check exists to catch).`n`n" +
       "Check manually: wsl -d Ubuntu-24.04 -- bash /home/anand/github/openalgo/signal_engine/scripts/openalgoctl.sh status"

try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    [System.Windows.Forms.MessageBox]::Show($msg, "OpenAlgo: possible outage", "OK", "Warning") | Out-Null
}
catch {
    # Last-resort fallback if Windows Forms isn't available for some reason.
    Write-EventLog -LogName Application -Source "Application" -EventId 1 -EntryType Warning -Message $msg -ErrorAction SilentlyContinue
}
