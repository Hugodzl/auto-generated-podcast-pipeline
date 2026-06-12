# The Random Rabbit Hole

Weekly pipeline: science RSS -> paper PDF -> NotebookLM audio -> Plex.

> **First time setting this up?** Follow the step-by-step
> [Manual Integration Checklist](docs/INTEGRATION.md) — it covers `nlm`
> first-contact discovery, credentials, Plex, the smoke test, and scheduling,
> with troubleshooting. The summary below is the short version.

## Setup (one-time)

1. Install [uv](https://docs.astral.sh/uv/) and Python 3.11+.
2. `uv sync`
3. Install the NotebookLM CLI: `npm install -g notebooklm-mcp-cli`
4. `nlm login` (opens browser; persists auth).
5. `copy config\config.example.toml config\config.toml` then edit topics/sources.
6. `uv run python -m rabbit_hole.setup` (enter Plex token + Discord webhook).
7. In Plex: confirm a **Music** library named "The Random Rabbit Hole" points at
   `D:\Videos\PlexLibrary\Podcasts\The Random Rabbit Hole`.
8. Dry run: `uv run python -m rabbit_hole.run` with `episodes_per_run = 1` in config.

## Running manually

A run takes ~10 minutes *per episode*. NEVER launch it as a child of an
interactive session you might close (terminal tab, Claude Code session, SSH) —
the process tree dies with the session (this killed run 5 mid-episode). Use the
detached launcher:

    powershell -ExecutionPolicy Bypass -File scripts\run_detached.ps1

It registers/overwrites a one-off Scheduled Task ("RabbitHoleManual") and starts
it immediately, fully detached. Watch progress with:

    Get-Content logs\rabbit_hole.log -Tail 5 -Wait

## Scheduling (Windows Task Scheduler)

Create a weekly task, Monday 07:00:

```
schtasks /Create /TN "RabbitHole" /SC WEEKLY /D MON /ST 07:00 ^
  /TR "\"%CD%\.venv\Scripts\pythonw.exe\" -m rabbit_hole.run" ^
  /RU %USERNAME%
```

Set "Start in" to the project directory so relative `config/` and `data/` paths resolve.

## Confirmed config (v1)

- Schedule: Monday 07:00, 5 episodes/run
- Ranking: source_weight x topic_score x recency (30-day half-life)
- Episode filenames: `{topic} - {title}.{ext}`
- Bilingual EN/FR, best-score-wins regardless of language

## Deferred to v1.1

Local config dashboard (edit topics/sources/weights/schedule via UI). Config is
already machine-writable + pydantic-validated to support this.
