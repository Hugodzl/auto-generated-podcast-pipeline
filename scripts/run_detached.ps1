# Launches the pipeline detached from the calling session via a one-off
# Scheduled Task, so closing the shell / Claude session / SSH cannot kill it.
# Native cmdlets (not schtasks.exe): typed parameters survive the space in the
# project path, and failures actually throw under ErrorActionPreference=Stop.
$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found: $python (run 'uv sync')" }

$action   = New-ScheduledTaskAction -Execute $python -Argument "-m rabbit_hole.run" -WorkingDirectory $root
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 4)
# No trigger on purpose: a `-Once -At (Get-Date).AddHours(24)` placeholder is NOT
# inert -- it leaves the task armed to RE-FIRE ~24h later and silently start a
# duplicate pipeline run (confirmed 2026-06-19). A trigger-less task can't
# auto-fire; Start-ScheduledTask runs it on demand right below.
Register-ScheduledTask -TaskName "RabbitHoleManual" -Action $action -Settings $settings -Force | Out-Null
Start-ScheduledTask    -TaskName "RabbitHoleManual"

Write-Host "Detached run started (Scheduled Task 'RabbitHoleManual')."
Write-Host "Watch:  Get-Content `"$root\logs\rabbit_hole.log`" -Tail 5 -Wait"
