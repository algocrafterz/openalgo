# -------------------------------------------------------
# OpenAlgo Service Controller (Windows)
#
# Usage:
#   .\openalgoctl.ps1 start    — start in background (returns after health check)
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

# -------- Usage --------

if (-not $Command) {

    Write-Host "Usage: .\openalgoctl.ps1 {start|run|stop|restart|status}" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  start    Start in background, return after health check"
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

# -------- Run command in WSL --------

function Invoke-Ctl {
    param([string]$cmd)

    # Verify project directory
    $dirCheck = & $wsl -d $distro -- bash -lc "[ -d $workdir ] && echo OK" 2>$null

    if (-not $dirCheck -or $dirCheck.Trim([char]0).Trim() -ne "OK") {
        Write-Log "ERROR: project directory not found: $workdir"
        exit 3
    }

    & $wsl `
        -d $distro `
        -- bash -lc "cd $workdir && $ctlScript $cmd" `
        2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ }
            Write-Log $line
            Write-Host $line
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

    Invoke-Ctl $Command

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
