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
    [ValidateSet("start", "run", "stop", "restart", "status")]
    [string]$Command
)

$ErrorActionPreference = "Stop"

# -------- Configuration --------

$log = "$PSScriptRoot\openalgoctl.log"
$wsl = "C:\Windows\System32\wsl.exe"
$distro = "Ubuntu-24.04"

$workdir = "/home/anand/github/openalgo"
$ctlScript = "./signal_engine/scripts/openalgoctl.sh"

$maxLogSizeMB = 5
$healthUrl = "http://127.0.0.1:5000/"
$maxWait = 90
$servicePidFile = "$PSScriptRoot\openalgo-service.pid"

# -------- Usage --------

if (-not $Command) {

    Write-Host "Usage: .\openalgoctl.ps1 {start|run|stop|restart|status}" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  start    Start in hidden window, return after health check"
    Write-Host "  run      Start in foreground, block until exit (Task Scheduler)"
    Write-Host "  stop     Stop all services"
    Write-Host "  restart  Stop then start"
    Write-Host "  status   Show running state"
    exit 1
}

# -------- Logging Function --------

function Write-Log {
    param([string]$msg)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$timestamp | $msg" | Out-File $log -Append -Encoding utf8
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

    # Kill old service window if it exists
    if (Test-Path $servicePidFile) {
        $oldPid = Get-Content $servicePidFile -ErrorAction SilentlyContinue
        if ($oldPid) {
            Write-Log "Killing old service window (PID $oldPid)..."
            taskkill /T /F /PID $oldPid 2>$null | Out-Null
        }
        Remove-Item $servicePidFile -Force -ErrorAction SilentlyContinue
    }

    # Stop services inside WSL
    Write-Log "Cleaning up stale state..."
    $ErrorActionPreference = "Continue"
    & $wsl -d $distro -- bash -lc "cd $workdir && $ctlScript stop" 2>&1 | Out-Null
    Start-Sleep -Seconds 2

    Write-Log "Launching OpenAlgo in minimized window (openalgoctl.sh run)..."

    # Write a batch file to avoid Start-Process argument quoting issues.
    # The batch file runs WSL in foreground — the window stays open as
    # long as services are running, and closes when they stop.
    $batFile = "$PSScriptRoot\openalgo-run.bat"
    @"
@echo off
title OpenAlgo Service
"$wsl" -d $distro -- bash -lc "cd $workdir && $ctlScript run"
"@ | Out-File -FilePath $batFile -Encoding ascii

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

        Write-Host "  Waiting... ($elapsed`s)" -ForegroundColor Gray
    }

    Write-Log "START FAILURE: Server not ready after $maxWait`s"
    Write-Host "Start failed. Check logs: $log" -ForegroundColor Red
    exit 1
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
            # Kill the service window
            if (Test-Path $servicePidFile) {
                $oldPid = Get-Content $servicePidFile -ErrorAction SilentlyContinue
                if ($oldPid) {
                    taskkill /T /F /PID $oldPid 2>$null | Out-Null
                }
                Remove-Item $servicePidFile -Force -ErrorAction SilentlyContinue
            }
        }

        "restart" {
            Invoke-Ctl "stop"
            if (Test-Path $servicePidFile) {
                $oldPid = Get-Content $servicePidFile -ErrorAction SilentlyContinue
                if ($oldPid) {
                    taskkill /T /F /PID $oldPid 2>$null | Out-Null
                }
                Remove-Item $servicePidFile -Force -ErrorAction SilentlyContinue
            }
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
