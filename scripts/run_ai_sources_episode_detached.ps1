# Launches the one-off AI-sources episode (scripts/make_ai_sources_episode.py) detached
# via a one-off Scheduled Task, so closing the shell / Claude session / SSH cannot kill
# it mid-generation. Mirrors run_topic_episode_detached.ps1; uses native cmdlets so the
# space in the project path survives and failures throw under ErrorActionPreference=Stop.
$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found: $python (run 'uv sync')" }

# 3 hours: this episode ingests 22 sources before generation starts, and each
# `source add --wait` is serial.
$action   = New-ScheduledTaskAction -Execute $python -Argument "scripts\make_ai_sources_episode.py" -WorkingDirectory $root
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 3)
# Deliberately NO trigger. run_detached.ps1 uses a `-Once -At (Get-Date).AddHours(24)`
# placeholder, but that leaves the task ARMED TO RE-FIRE ~24h later and silently
# generate a duplicate episode (confirmed 2026-06-19). A trigger-less task cannot
# auto-fire; Start-ScheduledTask runs it on demand right below.
Register-ScheduledTask -TaskName "RabbitHoleAiSourcesEpisode" -Action $action -Settings $settings -Force | Out-Null
Start-ScheduledTask    -TaskName "RabbitHoleAiSourcesEpisode"

Write-Host "Detached AI-sources-episode run started (Scheduled Task 'RabbitHoleAiSourcesEpisode')."
Write-Host "Watch:  Get-Content `"$root\logs\rabbit_hole.log`" -Tail 5 -Wait"
