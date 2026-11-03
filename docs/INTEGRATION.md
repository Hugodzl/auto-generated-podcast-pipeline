# Manual Integration Checklist

The automated v1 pipeline is complete and tested, but a few things can only be
wired up by hand on the real machine — installing/authenticating the `nlm` CLI,
confirming its actual command-line interface, storing secrets, and confirming
Plex behavior. Work through this list once; after that the weekly run is hands-off.

Run everything from the project root: `D:\Documents\Projets\NotebookLM Podcast`.
Use `uv run python ...` so the project virtualenv is used.

---

## 0. Environment prerequisite (one-time, already done once)

Both env vars below steer uv off `%APPDATA%`. Windows redirects `%APPDATA%` for
packaged (MSIX) apps into a private per-app store, which (a) caused a Python
junction error and (b) makes a `nlm` installed from inside the Claude desktop
app invisible to normal shells. Set them as persistent user env vars (run these
in a **normal** terminal, not inside a packaged app) and reopen the shell:

```
setx UV_PYTHON_INSTALL_DIR "D:\uv\python"
setx UV_TOOL_DIR "D:\uv\tools"
```

Then `uv sync` to ensure dependencies are installed.

---

## 1. Install and authenticate the `nlm` CLI

The CLI is the PyPI package **`notebooklm-mcp-cli`** (it provides the `nlm`
executable). It is a **Python** tool — install with `uv`, NOT npm. (The earlier
`npm install -g notebooklm-mcp-cli` was wrong: that name is not on npm.)

```
uv tool install notebooklm-mcp-cli
nlm --version          # confirms install (0.6.12+)
nlm doctor             # diagnoses install + auth + browser state
nlm login              # browser Google auth (interactive, one-time)
```

> The npm global install puts `nlm` at `%USERPROFILE%\.local\bin\nlm.exe` on
> Windows. Once it is on PATH, only `nlm login` remains.

- `nlm login` (auto mode) launches Chrome, you sign in to Google, and it caches
  the session under `~/.notebooklm-mcp-cli`.
- [ ] Confirm `nlm login` succeeds, then run `nlm login --check` — it should
      report the session is valid.
- [ ] **Headless persistence (the pipeline's #1 risk):** `nlm doctor` confirms a
      saved Chrome profile enables headless refresh, and the session survives
      across shells. BUT Google cookies expire in roughly **2–4 weeks**, so the
      weekly scheduled run will eventually hit a stale session needing a fresh
      `nlm login`.

> Stale-session detector: the orchestrator can run `nlm login --check` before
> generating (it reports invalid when stale) and fire the Discord alert so Hugo
> knows to re-run `nlm login`. Worth wiring into the run path.

---

## 2. The real `nlm` audio interface (discovered 2026-05-27)

`nlm` has no single "PDF → audio" command. Audio overviews are generated from a
**notebook's sources**, so the flow is multi-step. Verified via `nlm <cmd> --help`:

1. **Create a notebook:** `nlm notebook create "<title>"` → returns a notebook ID.
   (Robust ID capture: set a unique title, then match it via
   `nlm list notebooks --json` / `--quiet`.)
2. **Add the PDF and wait for ingest:**
   `nlm source add <notebook_id> --file <pdf_path> --wait --wait-timeout <secs>`
3. **Request the audio overview (async):**
   `nlm audio create <notebook_id> --format <format> --length <length> [--focus <topic>] --confirm`
   Format/length/focus come from the `[notebooklm]` config (defaults `deep_dive` /
   `default` / no focus) and can be overridden per request, e.g.
   `generate(paper, cfg.notebooklm, out, audio_length="long")`. `--focus` is omitted
   when empty.
   (Omitting `--source-ids` uses all of the notebook's sources, i.e. just our PDF.
   `audio create` has no `--wait`, so it returns before the audio is ready.)
4. **Poll until ready:** `nlm status artifacts <notebook_id> --json`, read the
   audio artifact's state in a loop, bounded by a timeout.
5. **Download:** `nlm download audio <notebook_id> --output <path>`. The default
   name is `./{notebook_id}_audio.m4a`, so **the real extension is `.m4a`**
   (already first in `AUDIO_EXTS`).
6. **Clean up:** `nlm notebook delete <notebook_id>` — each run creates a
   notebook, so delete it afterward (~260/year otherwise).

**Loudness boost (post-download, before filing):** NLM `.m4a`s are quiet (~-28 dB
mean), so `rabbit_hole.audio.boost_loudness` re-encodes each via ffmpeg
(`volume=<gain_factor>` then an `alimiter` true-peak ceiling at -1 dBFS). `gain_factor`
defaults to 1.4 (+40%) and is set in the `[audio]` config section. **ffmpeg + ffprobe
must be on PATH — a runtime dependency.** Boost failures are logged but non-fatal (the
episode is still filed). `scripts/boost_existing_episodes.py` applies the same boost to
episodes already in the Plex folder, backing each up to `data/audio_backup_pre_boost/`
first.

Auth: `nlm login` (browser, one-time) caches a session in `~/.notebooklm-mcp-cli`
that persists across shells (~2–4 wk before re-login). `nlm login --check`
validates it — use it as the stale-session detector.

Still to confirm on the first authed run (step 3 needs these real values):
- [ ] Exact stdout of `notebook create` (to parse the ID — else use the
      `list notebooks --json` + unique-title fallback).
- [ ] The JSON shape of `status artifacts --json` (which field carries the audio
      artifact's "ready" state and its artifact ID).
- [ ] Real generation time for a ~20-min episode (sets the poll timeout; the
      adapter's 1800s ceiling is probably fine).

---

## 3. Update `_run_nlm` to match reality

The flow in step 2 is confirmed multi-step, so implement all of it inside
`_run_nlm` in `rabbit_hole/notebooklm.py`. Keep the contract: take
`(nlm_command, pdf_path, out_dir)`, return the produced audio `Path` or `None`
on any failure (it already catches timeout / missing binary / non-zero exit;
wrap the new calls the same way, and delete the notebook in a `finally`).

- [ ] Implement create → `source add --file --wait` → `audio create --confirm`
      → poll `status artifacts --json` → `download audio --output
      <out_dir>/<name>.m4a`. Because `--output` names the file, write directly to
      the expected path instead of scanning `out_dir` for new files.
- [ ] `nlm notebook delete <id>` cleanup (best-effort) at the end.
- [ ] Do the real one-off run FIRST to capture the actual `notebook create`
      stdout and `status artifacts --json` shape, then code the parsing against
      reality (do not guess field names).
- [ ] Update `tests/test_notebooklm.py` — existing tests mock the subprocess
      seam; the multi-step flow needs new mocks. Re-run
      `uv run pytest tests/test_notebooklm.py -v`.

---

## 4. Store secrets (Windows Credential Manager)

```
uv run python -m rabbit_hole.setup
```

Prompts for, and stores under keyring service `rabbit_hole`:
- `plex_token` — your Plex auth token (X-Plex-Token)
- `discord_webhook_url` — webhook for failure alerts

- [ ] Run the helper.
- [ ] Verify: `uv run python -c "import keyring; print(bool(keyring.get_password('rabbit_hole','plex_token')))"` prints `True`.

> Finding your Plex token (Windows): Plex stores prefs in the **registry**, not a
> Preferences.xml. Read it with:
> `(Get-ItemProperty "HKCU:\Software\Plex, Inc.\Plex Media Server").PlexOnlineToken`
> (Ignore the `.LocalAdminToken` file — that's a different, local-only token.)
> Or use the web app: any item → ⋯ → Get Info → View XML; URL has `X-Plex-Token=...`.
>
> Discord webhook: Server Settings → Integrations → Webhooks → New Webhook →
> Copy URL.

---

## 5. Confirm the Plex library

Confirmed 2026-05-27 via the Plex API: the target is the existing **"Podcasts"**
library (key 4), Plex **type = Music** (`artist`) — despite the name, NOT the
Podcasts type — scanning `D:\Videos\PlexLibrary\Podcasts`, which already has a
`The Random Rabbit Hole\` subfolder. Episodes file there and are tagged
album="The Random Rabbit Hole" / artist="Hugo Déziel", so Plex groups them as
that album. Config therefore uses `library_name = "Podcasts"` and
`target_folder = "D:/Media/Podcasts/The Random Rabbit Hole"`.

### Track ordering and generation date

Each episode file is embedded with two additional tags at filing time:

- **`tracknumber`** — assigned by `next_track_number()` in `rabbit_hole/plex.py`
  as `max(existing tracknumber on disk) + 1`. Numbers ascend in creation order,
  so the newest episode is always the highest-numbered track. Plexamp (and Plex)
  sort by track number, giving a deterministic oldest-first playback order.
- **`date`** — set to the **generation day** (full ISO date, e.g. `2026-06-19`)
  via `date.today()` at filing time. This replaces the old behavior of using the
  source paper's publication year.

**One-time backfill for episodes already in Plex:**

```
uv run python scripts/number_existing_episodes.py
```

The script is re-runnable. It orders existing files by their DB `generated_at`
timestamp (falling back to file mtime for one-off episodes not in the DB), writes
the `tracknumber` and generation-`date` tags, refreshes Plex, and sets each
track's index via the Plex API as a safety net.

- [ ] (Optional) For a dedicated "The Random Rabbit Hole" library instead, create
      a **Music**-type library on that exact folder and set `library_name` to it —
      but don't nest it under the Podcasts library's path, or Plex indexes twice.

---

## 6. Create and edit your config

```
copy config\config.example.toml config\config.toml
```

- [ ] Review topics/keywords (your 3 buckets are pre-filled: AI studies; Animal
      behavior and cognition; Human relationships and psychology).
- [ ] Review sources/weights (Nature/Science 1.0 down to bioRxiv/PsyArXiv 0.6).
- [ ] **Set `episodes_per_run = 1`** for the first smoke test (raise to 5 later).
- [ ] Confirm `unpaywall_email = "you@example.com"`.

---

## 7. Smoke test (end-to-end, 1 episode)

```
uv run python -m rabbit_hole.run
```

- [ ] Watch `logs/rabbit_hole.log` (JSON lines): expect `ingested` → `resolved`
      → `ranked` → `filed` events.
- [ ] Confirm a PDF appeared under `data\pdfs\`.
- [ ] Confirm an audio file appeared in the Plex target folder, named
      `{topic} - {title}.{ext}`.
- [ ] Confirm it's tagged (artist = Hugo Déziel, album = The Random Rabbit Hole).
- [ ] Confirm the episode shows up in Plex after the refresh (give it a minute).
- [ ] Trigger a deliberate failure (e.g. temporarily wrong webhook) only if you
      want to confirm the Discord alert path — otherwise skip.

If nothing resolves: the v1 cascade is DOI→Unpaywall→Semantic Scholar only, so
some feeds may yield no open-access PDF. That's expected; Task 12 adds
arXiv/bioRxiv/HAL/Crossref to widen coverage.

---

## 8. Schedule the weekly run

Set `episodes_per_run` (>= 3, so `topic_round_robin` actually cycles topics)
in `config/config.toml`, then register the task (Monday 07:00 local):

```
powershell -File scripts\register_weekly_task.ps1
```

- [ ] The script sets the task's working directory to the project root, so the
      relative `config/`, `data/`, and `logs/` paths resolve. Registering by hand
      with `schtasks /TR` does not, and the run dies on a missing config.
- [ ] Run the task once manually from Task Scheduler to confirm it works under
      the scheduled context (not just your interactive shell).

---

## 9. After integration: tell Claude

Once the smoke test passes end-to-end, the next coding work is **Task 12**
(add arXiv/bioRxiv/HAL/Crossref resolver providers) — now informed by the real
API behavior you've seen. Bring back any notes from step 2 (the real `nlm`
interface) so the adapter and tests reflect reality.

---

## Troubleshooting quick reference

| Symptom | Likely cause / fix |
|---|---|
| `uv run` Python-install junction error | `setx UV_PYTHON_INSTALL_DIR "D:\uv\python"`, reopen shell |
| `nlm` → "uv trampoline failed to canonicalize script path" | `nlm` was installed into a redirected `%APPDATA%` location (happens when installed from inside the Claude/MSIX app) and isn't visible to this shell. Reinstall to `D:\`: `setx UV_TOOL_DIR "D:\uv\tools"`, reopen shell, `uv tool install --force notebooklm-mcp-cli`. (Installing from a normal terminal also avoids it.) |
| Run aborts after 5 episodes, Discord says "failed" | 5 consecutive `nlm` failures — likely `nlm` auth stale (run `nlm login --check`; if invalid, re-`nlm login`) or wrong command in `_run_nlm` |
| No episodes produced, no errors | No open-access PDF found for ranked papers (v1 cascade is narrow); widen via Task 12 |
| Audio file in folder but not in Plex | Plex refresh is best-effort; wait for Plex's own scan, or check library type is `Music` and token is valid |
| Episodes never appear / always re-aired | Check `data\state.db` `episodes` table is being written (record happens after filing) |
