# Launches the pipeline detached from the calling session via a one-off
# Scheduled Task, so closing the shell / Claude session / SSH cannot kill it.
# Native cmdlets (not schtasks.exe): typed parameters survive the space in the
# project path, and failures actually throw under ErrorActionPreference=Stop.
$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found: $python (run 'uv sync')" }

$action   = New-ScheduledTaskAction -Execute $python -Argument "-m rabbit_hole.run" -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(24)  # placeholder; never auto-fires today
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName "RabbitHoleManual" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask    -TaskName "RabbitHoleManual"

Write-Host "Detached run started (Scheduled Task 'RabbitHoleManual')."
Write-Host "Watch:  Get-Content `"$root\logs\rabbit_hole.log`" -Tail 5 -Wait"
