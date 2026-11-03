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
   the same folder as `[plex] target_folder` in your config — e.g.
   `D:\Media\Podcasts\The Random Rabbit Hole`.
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

## One-off / themed episodes

Separate from the weekly RSS run: build one episode from a curated reading list
(many PDFs and URLs) or from a single book PDF. These bypass poll/resolve/rank
and reuse the back end unchanged, so they cannot collide with the weekly run's
dedup or recovery.

An episode is a small `EpisodeSpec` in `scripts/make_<name>_episode.py` — title,
Plex bucket, source lists, and the focus brief. All the behaviour (fetch,
preflight, generate, boost, file) lives in `rabbit_hole/oneoff.py` and is covered
by `tests/test_oneoff.py`. To add an episode, copy a spec; don't copy machinery.

    uv run python scripts\make_topic_episode.py --fetch --check   # download sources, preflight
    uv run python scripts\make_topic_episode.py --check           # preflight only, no NotebookLM time
    uv run python scripts\make_topic_episode.py --check --no-probe # skip the nlm URL probe

Always `--check` first: it verifies the PDFs, ffmpeg, the Plex token, that the
`nlm` login is still valid, and — crucially — asks NotebookLM itself whether it
will ingest each URL source. HTTP reachability is the wrong oracle in *both*
directions (sites that 403 a plain client ingest fine; sites that answer 200 get
refused), and one refused URL aborts the notebook after everything ahead of it
has already ingested.

Then launch detached, never as a child of a session you might close:

    powershell -ExecutionPolicy Bypass -File scripts\run_oneoff_detached.ps1 topic

Valid names: `topic`, `ai-sources`, `knowledge-system`, `book`. Add
`-TimeLimitHours 4` for a long reading list.

**Length gotcha:** `--length long` is a ceiling, not a target, and source count is
the real lever — the same settings produced 46 min (1 PDF), 45 min (14 sources)
and 22 min (22 sources). For a long episode prefer *fewer* sources and a tighter
focus brief.

## Scheduling (Windows Task Scheduler)

Create (or replace) the weekly task, Monday 07:00:

```
powershell -File scripts\register_weekly_task.ps1
```

It sets `-WorkingDirectory` to the project root, so relative `config/`, `data/`
and `logs/` paths resolve; `schtasks /TR "%CD%\..."` does not, and the task then
fails to find the config. Task name: `RabbitHoleWeekly`.

## Confirmed config (v1)

- Schedule: Monday 07:00, 3 episodes/run (>=3 or `topic_round_robin` can't cycle)
- Ranking: source_weight x topic_score x recency (30-day half-life)
- Episode filenames: `{topic} - {title}.{ext}`
- Bilingual EN/FR, best-score-wins regardless of language

## Deferred to v1.1

Local config dashboard (edit topics/sources/weights/schedule via UI). Config is
already machine-writable + pydantic-validated to support this.
