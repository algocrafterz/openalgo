<#
.SYNOPSIS
  Holds the PC awake on trading days from 08:00 to 16:30 (BUG-024). Applies no power setting.

.DESCRIPTION
  Calls SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) every 30 seconds so Windows
  does not sleep (Modern Standby included) while the engine trades. The display may still turn
  off. At 16:30 it clears the request (ES_CONTINUOUS alone) and exits. On Saturday and Sunday it
  exits at once. NSE holidays are not known here: on a holiday the PC simply stays awake until
  16:30. Started after 16:30 it exits at once; started before 08:00 it waits until 08:00.

  LIMIT: a PC that is already asleep at 08:00 cannot run this task. The task's WakeToRun did not
  wake this PC on 2026-11-06 and 2026-11-09 (Modern Standby has no timed wake). Pair it with
  the persistent setting below, or start the PC awake by hand.

  REGISTER (owner, elevated PowerShell; the path is the WSL checkout seen from Windows, so copy
  the script to a Windows folder first if the \\wsl$ path is not reachable at logon):

    $script = 'C:\Users\<you>\keep-awake.ps1'   # copy of signal_engine\scripts\keep-awake.ps1
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
      -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:00
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -DontStopIfGoingOnBatteries `
      -AllowStartIfOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 9)
    Register-ScheduledTask -TaskName 'openAlgoKeepAwake' -Action $action -Trigger $trigger `
      -Settings $settings -RunLevel Limited -Description 'BUG-024 keep PC awake 08:00-16:30'

  AC power: the task runs on battery too (-AllowStartIfOnBatteries) so a laptop that was
  unplugged still stays awake; remove those two switches to run on AC power only.
  Remove: Unregister-ScheduledTask -TaskName openAlgoKeepAwake -Confirm:$false

  PERSISTENT SETTING (what actually prevents the 08:00 sleep; elevated PowerShell or cmd):
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
  Cost: the PC never sleeps on AC power (about 10-50 W idle depending on the machine, all day
  and night, plus fan and screen-off wear) until the setting is reverted:
    powercfg /change standby-timeout-ac 30
#>
param(
    [string]$StartAt = '08:00',
    [string]$StopAt = '16:30',
    [int]$PulseSeconds = 30
)

Add-Type -Namespace Win32 -Name Power -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@

$ES_CONTINUOUS = [uint32]2147483648      # 0x80000000
$ES_SYSTEM_REQUIRED = [uint32]1          # 0x00000001

function Get-Moment([string]$hhmm) {
    $parts = $hhmm.Split(':')
    return (Get-Date).Date.AddHours([int]$parts[0]).AddMinutes([int]$parts[1])
}

$now = Get-Date
if ($now.DayOfWeek -in 'Saturday', 'Sunday') { exit 0 }
$start = Get-Moment $StartAt
$stop = Get-Moment $StopAt
if ($now -ge $stop) { exit 0 }
if ($now -lt $start) { Start-Sleep -Seconds ([int]($start - $now).TotalSeconds) }

try {
    while ((Get-Date) -lt $stop) {
        # Returns the previous state, or 0 on failure. The request is re-asserted every pulse.
        [void][Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
        Start-Sleep -Seconds $PulseSeconds
    }
}
finally {
    [void][Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS)   # release the request
}
