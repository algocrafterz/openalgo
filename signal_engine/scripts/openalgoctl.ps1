# -------------------------------------------------------
# OpenAlgo Service Controller (Windows)
#
# Usage:
#   .\openalgoctl.ps1 start    — start in hidden window (returns after health check)
#   .\openalgoctl.ps1 run      — start in foreground (for Task Scheduler)
#   .\openalgoctl.ps1 stop     — stop all services
#   .\openalgoctl.ps1 restart  — stop then start
#   .\openalgoctl.ps1 status   — show running state
#
# Prerequisites:
#   1. Unblock this script if copied/downloaded (one-time):
#        Unblock-File -Path .\openalgoctl.ps1
#
#   2. Shell scripts must have Unix line endings (LF, not CRLF).
#      If you get "bash\r: No such file or directory", fix with:
#        wsl -d Ubuntu-24.04 -- bash -c "sed -i 's/\r$//' /home/anand/github/openalgo/signal_engine/scripts/*.sh"
#      Or configure git to keep LF in WSL:
#        git config core.autocrlf input
# -------------------------------------------------------

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "run", "stop", "restart", "status", "squareoff")]
    [string]$Command
)

$ErrorActionPreference = "Stop"

# -------- Configuration --------

$log = "$PSScriptRoot\openalgoctl.log"
$wsl = "C:\Windows\System32\wsl.exe"
$distro = "Ubuntu-24.04"

$workdir = "/home/anand/github/openalgo"
# Invoked as "bash <script>", not "./<script>" — the WSL executable bit on
# this file has been lost before (git checkout/edit reset it to non-exec),
# which makes "./openalgoctl.sh" fail instantly with Permission Denied
# before it can log or alert anything. "bash <script>" only needs read
# access, so it survives that class of failure entirely.
$ctlScript = "bash ./signal_engine/scripts/openalgoctl.sh"

$maxLogSizeMB = 5
$healthUrl = "http://127.0.0.1:5000/"
$maxWait = 90
$servicePidFile = "$PSScriptRoot\openalgo-service.pid"

# -------- Usage --------

if (-not $Command) {

    Write-Host "Usage: .\openalgoctl.ps1 {start|run|stop|restart|status|squareoff}" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  start      Start in hidden window, return after health check"
    Write-Host "  run        Start in foreground, block until exit (Task Scheduler)"
    Write-Host "  stop       Stop all services"
    Write-Host "  restart    Stop then start"
    Write-Host "  status     Show running state"
    Write-Host "  squareoff  Cancel orders and close all MIS positions (3:02 PM failsafe)"
    exit 1
}

# -------- Logging Function --------

function Write-Log {
    param([string]$msg)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$timestamp | $msg"

    # Out-File can hit a transient sharing violation when another
    # openalgoctl.ps1 invocation (e.g. the heartbeat task) appends to the
    # same log at the same instant. Retry briefly instead of letting the
    # exception propagate to the top-level catch, which used to abort the
    # entire start/restart command over a single log line.
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            $line | Out-File $log -Append -Encoding utf8
            return
        }
        catch {
            if ($attempt -eq 5) {
                Write-Host "$line (log write failed after retries: $($_.Exception.Message))" -ForegroundColor Yellow
                return
            }
            Start-Sleep -Milliseconds 100
        }
    }
}

# -------- Log Rotation --------

try {

    if (Test-Path $log) {

        $sizeMB = (Get-Item $log).Length / 1MB

        if ($sizeMB -gt $maxLogSizeMB) {

            $backup = "$log.old"

            if (Test-Path $backup) {
                Remove-Item $backup -Force
            }

            Rename-Item $log $backup -Force
        }
    }

}
catch {}

# -------- WSL Preflight (shared by all commands) --------

function Test-WslReady {

    # Verify WSL exists
    if (!(Test-Path $wsl)) {
        Write-Log "ERROR: wsl.exe not found"
        return $false
    }

    # Verify distro exists (handle UTF-16LE BOM from wsl -l -q)
    $distros = & $wsl -l -q 2>$null | ForEach-Object { $_.Trim([char]0).Trim() } | Where-Object { $_ -ne "" }

    if ($distros -notcontains $distro) {
        Write-Log "ERROR: WSL distro '$distro' not installed. Available: $($distros -join ', ')"
        return $false
    }

    # WSL readiness retry loop
    $maxRetries = 5
    $retryDelay = 5
    $attempt = 0

    while ($attempt -lt $maxRetries) {

        try {
            $result = & $wsl -d $distro -- echo ready 2>$null

            if ($result -and $result.Trim([char]0).Trim() -eq "ready") {
                return $true
            }
        }
        catch {}

        $attempt++
        Write-Log "WSL not ready, retry $attempt/$maxRetries"
        Start-Sleep -Seconds $retryDelay
    }

    Write-Log "ERROR: WSL did not become ready"
    return $false
}

# -------- Run command in WSL (foreground, captures output) --------

function Invoke-Ctl {
    param([string]$cmd)

    # Verify project directory
    $dirCheck = & $wsl -d $distro -- bash -lc "[ -d $workdir ] && echo OK" 2>$null

    if (-not $dirCheck -or $dirCheck.Trim([char]0).Trim() -ne "OK") {
        Write-Log "ERROR: project directory not found: $workdir"
        exit 3
    }

    # Use Continue — Python logging writes to stderr, which PowerShell
    # wraps as ErrorRecord. With "Stop" these become terminating exceptions.
    $ErrorActionPreference = "Continue"

    & $wsl `
        -d $distro `
        -- bash -lc "cd $workdir && $ctlScript $cmd" `
        2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ }
            Write-Log $line
            Write-Host $line
        }

    # Check exit code from WSL
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "openalgoctl.sh $cmd failed with exit code $LASTEXITCODE"
    }
}

# -------- Start: launch 'run' in a hidden window, poll health --------

function Invoke-Start {

    # Check if already running
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($response.StatusCode -eq 200) {
            Write-Log "START SKIPPED: OpenAlgo already running at $healthUrl"
            Write-Host "OpenAlgo already running." -ForegroundColor Yellow
            return
        }
    }
    catch {}

    # NOTE: this used to unconditionally taskkill any old service window and
    # call 'openalgoctl.sh stop' here before every relaunch — on every
    # watchdog tick, whether or not the existing instance was actually dead.
    # If a previous start was merely slow (NTP wait, broker login — can run
    # 1-3 minutes) rather than dead, this killed a perfectly good in-progress
    # boot and relaunched it, which is exactly the kill/relaunch churn that
    # made overlapping starts messy (see 2026-09-15 postmortem in
    # openalgoctl.log). The health check above already skips cleanly when
    # truly up. For every other case, openalgoctl.sh's own flock-based lock
    # (acquire_lock() in openalgoctl.sh) is now the single source of truth:
    # if a start/run is already in flight anywhere — this window, a stale
    # window, or a direct WSL invocation — the shell script refuses the
    # duplicate itself, logs why, and notifies via Telegram. Nothing here
    # needs to kill anything pre-emptively anymore.
    if (Test-Path $servicePidFile) {
        $oldPid = Get-Content $servicePidFile -ErrorAction SilentlyContinue
        if (-not ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue))) {
            # Stale reference to a window that's already gone — just clean up the file.
            Remove-Item $servicePidFile -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Log "Launching OpenAlgo in minimized window (openalgoctl.sh run)..."

    # Write a batch file to avoid Start-Process argument quoting issues.
    # The batch file runs WSL in foreground — the window stays open as
    # long as services are running, and closes when they stop.
    #
    # Written to $env:TEMP (a local NTFS path), NOT $PSScriptRoot. This
    # script lives inside the WSL filesystem, reached from Windows via the
    # \\wsl.localhost\... UNC path — a batch file launched via Start-Process
    # from that "network" location gets Windows' Mark-of-the-Web / SmartScreen
    # "Open File - Security Warning" modal EVERY time, which blocks unattended
    # execution and needs a human to click "Run". A file freshly written to a
    # genuine local path by the current user isn't flagged that way.
    $batFile = "$env:TEMP\openalgo-run.bat"
    @"
@echo off
title OpenAlgo Service
"$wsl" -d $distro -- bash -lc "cd $workdir && $ctlScript run"
"@ | Out-File -FilePath $batFile -Encoding ascii
    Unblock-File -Path $batFile -ErrorAction SilentlyContinue

    $proc = Start-Process -WindowStyle Minimized -FilePath $batFile -PassThru
    $proc.Id | Out-File $servicePidFile -Encoding ascii
    Write-Log "Service window PID: $($proc.Id)"

    Write-Host "Waiting for OpenAlgo to start..." -ForegroundColor Cyan

    # Poll health URL
    $ErrorActionPreference = "Stop"
    $elapsed = 0

    while ($elapsed -lt $maxWait) {

        Start-Sleep -Seconds 2
        $elapsed += 2

        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
            if ($response.StatusCode -eq 200) {
                Write-Log "START SUCCESS: OpenAlgo running at $healthUrl"
                Write-Host "OpenAlgo started successfully." -ForegroundColor Green
                return
            }
        }
        catch {}

        # The window's cmd.exe waits on wsl.exe and only exits once
        # openalgoctl.sh exits — so an early exit here almost always means
        # the single-instance lock refused this as a duplicate (see
        # acquire_lock() in openalgoctl.sh) rather than a genuinely slow
        # boot. Stop polling and point at the real reason instead of waiting
        # out the rest of $maxWait for nothing.
        if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
            Write-Log "START ABORTED: service window exited before health check passed — see openalgoctl.log for the reason (likely a duplicate-start refusal)."
            Write-Host "Start did not complete — window exited early. Check signal_engine/logs/openalgoctl.log for the reason." -ForegroundColor Red
            exit 1
        }

        Write-Host "  Waiting... ($elapsed`s)" -ForegroundColor Gray
    }

    Write-Log "START FAILURE: Server not ready after $maxWait`s"
    Write-Host "Start failed. Check logs: $log" -ForegroundColor Red
    exit 1
}

# -------- Kill service window (reads PID file, kills process, removes file) --------

function Stop-ServiceWindow {

    if (Test-Path $servicePidFile) {
        $oldPid = Get-Content $servicePidFile -ErrorAction SilentlyContinue
        if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
            Write-Log "Killing service window (PID $oldPid)..."
            try { & taskkill /T /F /PID $oldPid 2>&1 | Out-Null } catch { Write-Log "taskkill ignored: $($_.Exception.Message)" }
        }
        Remove-Item $servicePidFile -Force -ErrorAction SilentlyContinue
    }
}

# -------- Main Execution --------

try {

    Write-Log "========== $($Command.ToUpper()) =========="

    if (!(Test-WslReady)) {
        Write-Host "WSL is not ready. Check log: $log" -ForegroundColor Red
        exit 4
    }

    Write-Log "WSL ready, distro: $distro"

    switch ($Command) {

        "start" {
            Invoke-Start
        }

        "stop" {
            Invoke-Ctl "stop"
            Stop-ServiceWindow
        }

        "restart" {
            Invoke-Ctl "stop"
            Stop-ServiceWindow
            Start-Sleep -Seconds 3
            Invoke-Start
        }

        default {
            # run, status — pass through to WSL
            Invoke-Ctl $Command
        }
    }

    Write-Log "Command '$Command' completed"

}
catch {

    Write-Log "FATAL ERROR: $($_.Exception.Message)"
    Write-Host "FATAL ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 100

}
finally {

    Write-Log "=========== END ==========="

}
