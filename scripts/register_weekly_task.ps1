# Registers the weekly pipeline run as a Scheduled Task (Monday 07:00, matching
# config.toml's [schedule] cron). Re-run it any time; -Force replaces in place.
#
# Native cmdlets rather than schtasks.exe, for the same reasons as run_detached.ps1:
# a typed -WorkingDirectory survives the space in the project path (relative
# config/ and data/ paths break without it), and failures throw instead of
# printing to stdout and returning 0.
$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found: $python (run 'uv sync')" }

$action  = New-ScheduledTaskAction -Execute $python -Argument "-m rabbit_hole.run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 7am
# StartWhenAvailable: this is a desktop, not a server -- a Monday the machine was
# off would otherwise silently skip a week. ExecutionTimeLimit covers 3 episodes
# at ~40 min of NotebookLM each plus resolve/boost/file overhead.
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
                                         -StartWhenAvailable `
                                         -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "RabbitHoleWeekly" -Action $action -Trigger $trigger `
                       -Settings $settings -Force | Out-Null

$t = Get-ScheduledTask -TaskName "RabbitHoleWeekly"
$next = (Get-ScheduledTaskInfo -TaskName "RabbitHoleWeekly").NextRunTime
Write-Host "Registered 'RabbitHoleWeekly' ($($t.State)). Next run: $next"
Write-Host "Run it now with:  Start-ScheduledTask -TaskName RabbitHoleWeekly"
