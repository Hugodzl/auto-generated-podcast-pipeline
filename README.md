# The Random Rabbit Hole

Weekly pipeline: science RSS -> paper PDF -> NotebookLM audio -> Plex.

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
