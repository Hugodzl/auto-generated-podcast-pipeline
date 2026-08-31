# Launches any one-off / themed episode detached via a one-off Scheduled Task, so
# closing the shell / Claude session / SSH cannot kill it mid-generation.
#
#   .\scripts\run_oneoff_detached.ps1 knowledge-system
#   .\scripts\run_oneoff_detached.ps1 ai-sources -TimeLimitHours 4
#
# Replaces the four near-identical launchers (run_topic_episode_detached.ps1,
# run_ai_sources_episode_detached.ps1, run_knowledge_system_episode_detached.ps1,
# run_book_episode_detached.ps1). Task names are unchanged, so anything watching
# for "RabbitHoleKnowledgeSystemEpisode" still works.
#
# run_detached.ps1 is a different thing and stays: it runs the weekly RSS pipeline
# (-m rabbit_hole.run), not a one-off episode.
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("topic", "ai-sources", "knowledge-system", "book")]
    [string]$Episode,

    # Ingestion is serial (~10-30s per source) and generation is 15-45 min on top.
    # 3h clears a 20-source reading list comfortably; raise it for a longer one.
    [ValidateRange(1, 12)]
    [int]$TimeLimitHours = 3
)
$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found: $python (run 'uv sync')" }

$slug       = $Episode -replace '-', '_'
$scriptPath = "scripts\make_${slug}_episode.py"
if (-not (Test-Path (Join-Path $root $scriptPath))) {
    throw "no such episode script: $scriptPath"
}

# knowledge-system -> RabbitHoleKnowledgeSystemEpisode. These are the task names
# the old per-episode launchers registered, kept byte-identical on purpose.
$pascal = ($Episode -split '-' | ForEach-Object {
    $_.Substring(0, 1).ToUpper() + $_.Substring(1)
}) -join ''
$task = "RabbitHole${pascal}Episode"

$action   = New-ScheduledTaskAction -Execute $python -Argument $scriptPath -WorkingDirectory $root
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours $TimeLimitHours)
# Deliberately NO trigger. A `-Once -At (Get-Date).AddHours(24)` placeholder is NOT
# inert -- it leaves the task ARMED TO RE-FIRE ~24h later and silently generate a
# duplicate episode (confirmed 2026-06-19). A trigger-less task cannot auto-fire;
# Start-ScheduledTask runs it on demand right below.
Register-ScheduledTask -TaskName $task -Action $action -Settings $settings -Force | Out-Null
Start-ScheduledTask    -TaskName $task

Write-Host "Detached $Episode-episode run started (Scheduled Task '$task', limit ${TimeLimitHours}h)."
Write-Host "Watch:  Get-Content `"$root\logs\rabbit_hole.log`" -Tail 5 -Wait"
