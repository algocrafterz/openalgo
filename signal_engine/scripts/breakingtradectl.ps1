# -------------------------------------------------------
# Windows control wrapper for the BreakingTrade scanner poller.
#
# Mirrors openalgoctl.ps1: reaches into WSL and drives the Linux-side control
# script. Kept SEPARATE from openalgoctl.ps1 on purpose - the poller is a data
# collector, not a trading service, and it must be startable and stoppable
# without touching anything that places orders.
#
#   .\breakingtradectl.ps1 start    Start the poller (idempotent - safe to repeat)
#   .\breakingtradectl.ps1 stop     Stop it
#   .\breakingtradectl.ps1 status   Report whether it is really running
#   .\breakingtradectl.ps1 audit    Show scheduled vs collected polls for today
#
# 'start' is what both the AutoStart task and the Watchdog call. It returns
# immediately; the poller runs detached inside WSL under its own PID file, so
# nothing here needs to stay open.
# -------------------------------------------------------

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "audit")]
    [string]$Command
)

$ErrorActionPreference = "Stop"

# -------- Configuration (matches openalgoctl.ps1) --------

$log = "$PSScriptRoot\breakingtradectl.log"
$wsl = "C:\Windows\System32\wsl.exe"
$distro = "Ubuntu-24.04"

$workdir = "/home/anand/github/openalgo"
# Invoked as "bash <script>", not "./<script>" -- both poller.sh and scan.sh
# are tracked in git as mode 100644 (non-executable), same as openalgoctl.sh
# was before it silently broke every scheduled start on 2026-09-15/16. "bash
# <script>" only needs read access, so it can't fail that way.
$ctlScript = "bash ./signal_engine/analysis/breakingtrade/poller.sh"
$scanScript = "bash ./signal_engine/analysis/breakingtrade/scan.sh"

$maxLogSizeMB = 5

# -------- Usage --------

if (-not $Command) {
    Write-Host "Usage: .\breakingtradectl.ps1 {start|stop|status|audit}" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  start   Start the scanner poller (idempotent)"
    Write-Host "  stop    Stop the poller"
    Write-Host "  status  Report whether it is running"
    Write-Host "  audit   Scheduled vs collected polls for today"
    exit 1
}

# -------- Logging --------

function Write-Log {
    param([string]$msg)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $log -Value $line
    Write-Host $msg
}

if ((Test-Path $log) -and ((Get-Item $log).Length / 1MB -gt $maxLogSizeMB)) {
    Move-Item $log "$log.old" -Force
}

# -------- Main --------

try {
    if (!(Test-Path $wsl)) {
        Write-Log "ERROR: wsl.exe not found at $wsl"
        exit 2
    }

    # Handle the UTF-16LE BOM that `wsl -l -q` emits, same as openalgoctl.ps1.
    $distros = & $wsl -l -q 2>$null | ForEach-Object { $_.Trim([char]0).Trim() } | Where-Object { $_ -ne "" }
    if ($distros -notcontains $distro) {
        Write-Log "ERROR: WSL distro '$distro' not installed. Available: $($distros -join ', ')"
        exit 3
    }

    # Touching WSL here is also what keeps the VM ALIVE: WSL shuts down shortly after
    # its last process exits, and cron inside WSL dies with it. This is the difference
    # between "the laptop is on" and "collection is actually running".
    $ready = & $wsl -d $distro -- echo ready 2>$null
    if ($ready -notmatch "ready") {
        Write-Log "ERROR: WSL distro '$distro' did not respond"
        exit 4
    }

    switch ($Command) {
        "audit" {
            Write-Log "Running collection audit"
            & $wsl -d $distro -- bash -lc "cd $workdir && $scanScript --audit"
        }
        default {
            Write-Log "poller.sh $Command"
            & $wsl -d $distro -- bash -lc "cd $workdir && $ctlScript $Command"
        }
    }

    Write-Log "Command '$Command' completed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}
catch {
    Write-Log "FATAL ERROR: $($_.Exception.Message)"
    exit 1
}
