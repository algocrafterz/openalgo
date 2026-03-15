# -------------------------------------------------------
# OpenAlgo AutoStart Runtime Script
# -------------------------------------------------------

$ErrorActionPreference = "Stop"

# -------- Configuration --------

$log = "$PSScriptRoot\openalgo-autostart.log"
$wsl = "C:\Windows\System32\wsl.exe"
$distro = "Ubuntu-24.04"

$workdir = "/home/anand/github/openalgo"
$startScript = "./signal_engine/scripts/bootstrap.sh"

$maxLogSizeMB = 5

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

# -------- Main Execution --------

try {

    Write-Log "========== START =========="

    # Verify WSL exists
    if (!(Test-Path $wsl)) {

        Write-Log "ERROR: wsl.exe not found"
        exit 1
    }

    Write-Log "WSL binary verified"

    # Verify distro exists (handle UTF-16LE BOM from wsl -l -q)
    $distros = & $wsl -l -q 2>$null | ForEach-Object { $_.Trim([char]0).Trim() } | Where-Object { $_ -ne "" }

    if ($distros -notcontains $distro) {

        Write-Log "ERROR: WSL distro '$distro' not installed. Available: $($distros -join ', ')"
        exit 2
    }

    Write-Log "WSL distro verified: $distro"

    # -------- WSL Readiness Retry Loop --------

    $maxRetries = 5
    $retryDelay = 5
    $attempt = 0

    while ($attempt -lt $maxRetries) {

        try {

            $result = & $wsl -d $distro -- echo ready 2>$null

            if ($result -and $result.Trim([char]0).Trim() -eq "ready") {

                Write-Log "WSL ready"
                break
            }

        }
        catch {}

        $attempt++

        Write-Log "WSL not ready, retry $attempt/$maxRetries"

        Start-Sleep -Seconds $retryDelay
    }

    if ($attempt -eq $maxRetries) {

        Write-Log "ERROR: WSL did not become ready"
        exit 4
    }

    # -------- Verify project directory --------

    $dirCheck = & $wsl -d $distro -- bash -lc "[ -d $workdir ] && echo OK" 2>$null

    if (-not $dirCheck -or $dirCheck.Trim([char]0).Trim() -ne "OK") {

        Write-Log "ERROR: project directory not found: $workdir"
        exit 3
    }

    Write-Log "Project directory verified"

    # -------- Prevent duplicate start --------

    $existingProcess = & $wsl -d $distro -- bash -lc "pgrep -f 'uv run app.py'" 2>$null

    if ($existingProcess) {

        Write-Log "OpenAlgo already running (PID $($existingProcess.Trim([char]0).Trim()))"
        exit 0
    }

    Write-Log "Launching OpenAlgo..."

    # -------- Launch OpenAlgo --------

    & $wsl `
        -d $distro `
        -- bash -lc "cd $workdir && $startScript" `
        2>&1 | ForEach-Object { Write-Log $_ }

    Write-Log "OpenAlgo execution finished"

}
catch {

    Write-Log "FATAL ERROR: $($_.Exception.Message)"
    exit 100

}
finally {

    Write-Log "=========== END ==========="

}
