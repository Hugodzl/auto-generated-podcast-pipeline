# Launches the pipeline detached from the calling session via a one-off
# Scheduled Task, so closing the shell / Claude session / SSH cannot kill it.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found: $python (run 'uv sync')" }
$tr = "cmd /c cd /d `"$root`" && `"$python`" -m rabbit_hole.run"
schtasks /Create /TN "RabbitHoleManual" /SC ONCE /ST 23:59 /TR $tr /F | Out-Null
schtasks /Run /TN "RabbitHoleManual" | Out-Null
Write-Host "Detached run started (Scheduled Task 'RabbitHoleManual')."
Write-Host "Watch:  Get-Content `"$root\logs\rabbit_hole.log`" -Tail 5 -Wait"
