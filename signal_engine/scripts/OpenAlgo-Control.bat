@echo off
setlocal EnableExtensions
title OpenAlgo Control

rem Click-and-run control for a local OpenAlgo stack (app.py + signal engine, inside WSL).
rem Double-click for a menu, or pass one word: start | stop | restart | status | ui
rem
rem Calls openalgoctl.ps1 directly. It never uses the Task Scheduler tasks, so it cannot
rem leave a running task instance that blocks the next 08:50 openAlgoAutoStart (2026-10-16).
rem
rem Keep this file next to openalgoctl.ps1 (C:\Users\Anand\Scripts). Save it with CRLF line
rem endings: cmd.exe mis-reads labels in a file with LF-only endings.

set "CTL=%~dp0openalgoctl.ps1"
set "URL=http://127.0.0.1:5000/"

if not exist "%CTL%" (
    echo openalgoctl.ps1 was not found next to this file:
    echo   %CTL%
    pause
    exit /b 1
)

if /i "%~1"=="start"   goto do_start
if /i "%~1"=="stop"    goto do_stop
if /i "%~1"=="restart" goto do_restart
if /i "%~1"=="status"  goto do_status
if /i "%~1"=="ui"      goto do_ui

:menu
cls
echo ================================================
echo   OpenAlgo control   local, no scheduled task
echo ================================================
call :show_state
echo.
echo   1  Start
echo   2  Stop
echo   3  Restart
echo   4  Status
echo   5  Open web UI
echo   0  Exit
echo.
choice /c 123450 /n /m "Choose 1-5, or 0 to exit: "
if errorlevel 6 goto done
if errorlevel 5 goto do_ui_menu
if errorlevel 4 goto do_status_menu
if errorlevel 3 goto do_restart_menu
if errorlevel 2 goto do_stop_menu
if errorlevel 1 goto do_start_menu
goto menu

:do_start_menu
call :run_start
pause
goto menu

:do_stop_menu
call :confirm Stop
if errorlevel 1 goto menu
call :run_stop
pause
goto menu

:do_restart_menu
call :confirm Restart
if errorlevel 1 goto menu
call :run_restart
pause
goto menu

:do_status_menu
call :run_status
pause
goto menu

:do_ui_menu
start "" "%URL%"
goto menu

:do_start
call :run_start
goto done_pause
:do_stop
call :confirm Stop
if errorlevel 1 goto done
call :run_stop
goto done_pause
:do_restart
call :confirm Restart
if errorlevel 1 goto done
call :run_restart
goto done_pause
:do_status
call :run_status
goto done_pause
:do_ui
start "" "%URL%"
goto done

:done_pause
pause
:done
endlocal
exit /b 0

rem ---------------------------------------------------------------- actions

:run_start
echo.
echo Starting OpenAlgo. This takes about a minute: login, app, signal engine...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" start
exit /b 0

:run_stop
echo.
echo Stopping OpenAlgo...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" stop
exit /b 0

:run_restart
echo.
echo Restarting OpenAlgo. This takes about a minute...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" restart
exit /b 0

:run_status
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" status
call :show_state
exit /b 0

rem ---------------------------------------------------------------- helpers

:show_state
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:5000/health/status' -UseBasicParsing -TimeoutSec 3; Write-Host ('  App on port 5000 : UP (HTTP ' + $r.StatusCode + ')') } catch { Write-Host '  App on port 5000 : DOWN' }"
exit /b 0

rem confirm <Stop|Restart>: exit code 0 = go ahead, 1 = cancelled.
rem During weekday market hours (09:00 to 15:30 local) the warning is stronger.
:confirm
powershell -NoProfile -Command "$n = Get-Date; $t = $n.TimeOfDay; if (($n.DayOfWeek -ne 'Saturday') -and ($n.DayOfWeek -ne 'Sunday') -and ($t -ge [timespan]'09:00:00') -and ($t -lt [timespan]'15:30:00')) { exit 1 } else { exit 0 }"
if errorlevel 1 goto confirm_market
echo.
choice /c YN /n /m "%~1 OpenAlgo now? Press Y to continue, N to cancel: "
if errorlevel 2 exit /b 1
exit /b 0

:confirm_market
echo.
echo   WARNING: the market is open.
echo   %~1 stops the signal engine. Positions and their stop orders stay at the
echo   broker, but nothing tracks them, exits them or moves them until the engine is
echo   back. Do not do this while trades are open unless you mean to.
echo.
choice /c YN /n /m "%~1 anyway? Press Y to continue, N to cancel: "
if errorlevel 2 exit /b 1
exit /b 0
