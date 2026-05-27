# Manual Integration Checklist

The automated v1 pipeline is complete and tested, but a few things can only be
wired up by hand on the real machine — installing/authenticating the `nlm` CLI,
confirming its actual command-line interface, storing secrets, and confirming
Plex behavior. Work through this list once; after that the weekly run is hands-off.

Run everything from the project root: `D:\Documents\Projets\NotebookLM Podcast`.
Use `uv run python ...` so the project virtualenv is used.

---

## 0. Environment prerequisite (one-time, already done once)

If `uv run` fails with a Python-install **junction error**, set this persistent
user env var and reopen the shell:

```
setx UV_PYTHON_INSTALL_DIR "D:\uv\python"
```

Then `uv sync` to ensure dependencies are installed.

---

## 1. Install and authenticate the `nlm` CLI

```
npm install -g notebooklm-mcp-cli
nlm --version
nlm login
```

- `nlm login` should open a browser for Google auth and persist a session.
- [ ] Confirm `nlm login` succeeds.
- [ ] **Verify auth persists across shells:** open a NEW terminal and run a
      read-only `nlm` command (e.g. `nlm --help` or a list command). If it
      requires re-login each time, note how the session is stored (cookie file?
      token?) — the pipeline runs headless via Task Scheduler and needs auth to
      survive between weekly runs without a browser.

> If auth does NOT persist, this is the biggest risk to the whole pipeline.
> Capture how `nlm` stores credentials and where, so we can make the scheduled
> run reuse them (and add a "session stale" detector + Discord alert).

---

## 2. Discover the real `nlm` audio interface

The adapter `rabbit_hole/notebooklm.py` currently **assumes** this invocation
(a placeholder, isolated in the `_run_nlm` function):

```
nlm audio --source <pdf_path> --out <out_dir>
```

This is almost certainly not exactly right. Find the real one:

```
nlm --help
nlm audio --help        # or whatever the audio/overview subcommand is called
```

Capture answers to these — they drive the code change in step 3:

- [ ] What is the actual subcommand to generate an audio overview from a PDF?
      (e.g. `audio`, `generate`, `overview`, `notebook ... audio` ...)
- [ ] How is the input PDF passed? (a `--source` flag? positional arg? must the
      PDF first be uploaded to a notebook, then audio requested?)
- [ ] Where does the output land — a path you specify (`--out`), the cwd, or a
      download you must fetch separately?
- [ ] What is the output file's **actual extension**? (`.wav`? `.m4a`? `.mp3`?
      Hugo previously guessed `.m4v`.) The current `AUDIO_EXTS` tuple is
      `(".m4a", ".wav", ".mp3", ".m4v")` — add the real one if it's not there.
- [ ] Is there a **notebook lifecycle**? (Does each audio generation create a
      notebook that should be deleted afterward, or is there one reusable
      notebook? This affects long-term clutter in your NotebookLM account.)
- [ ] Roughly how long does one ~20-min episode take to generate? (The adapter's
      subprocess timeout is 1800s / 30 min — raise it if generation is slower.)

---

## 3. Update `_run_nlm` to match reality

Edit `rabbit_hole/notebooklm.py`, function `_run_nlm`, with the real command
discovered in step 2. The function's contract must stay the same: take
`(nlm_command, pdf_path, out_dir)`, run the CLI, return the produced audio
`Path` or `None` on any failure (it already catches timeout / missing binary /
non-zero exit). If the real flow is multi-step (upload → request → download),
implement all of it inside this one function.

- [ ] Update the subprocess call and, if needed, `AUDIO_EXTS`.
- [ ] If a notebook is created per run, add cleanup at the end of `_run_nlm`.
- [ ] Re-run `uv run pytest tests/test_notebooklm.py -v` (the existing tests mock
      the seam, so they should still pass; adjust only if you change the signature).
- [ ] Do a real one-off generation against a sample PDF and confirm an audio file
      appears where the code expects it.

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

> Finding your Plex token: in the Plex web app, open any item → ⋯ → Get Info →
> View XML; the URL contains `X-Plex-Token=...`. Or see Plex's support article
> "Finding an authentication token".
>
> Discord webhook: Server Settings → Integrations → Webhooks → New Webhook →
> Copy URL.

---

## 5. Confirm the Plex library

- [ ] In Plex: **Settings → Manage → Libraries**. Confirm a library named exactly
      **"The Random Rabbit Hole"** exists.
- [ ] Its **type must be `Music`** (NOT `Podcasts` — Plex's Podcasts type only
      handles external RSS, not local files).
- [ ] Its folder is `D:\Videos\PlexLibrary\Podcasts\The Random Rabbit Hole`
      (matching `target_folder` in `config/config.toml`).

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

Set `episodes_per_run = 5` in `config/config.toml`, then register the task
(Monday 07:00 local):

```
schtasks /Create /TN "RabbitHole" /SC WEEKLY /D MON /ST 07:00 ^
  /TR "\"%CD%\.venv\Scripts\pythonw.exe\" -m rabbit_hole.run" ^
  /RU %USERNAME%
```

- [ ] In Task Scheduler, set the task's **"Start in"** field to the project
      directory so relative `config/`, `data/`, and `logs/` paths resolve.
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
| Run aborts after 5 episodes, Discord says "failed" | 5 consecutive `nlm` failures — likely `nlm` auth stale (re-`nlm login`) or wrong command in `_run_nlm` |
| No episodes produced, no errors | No open-access PDF found for ranked papers (v1 cascade is narrow); widen via Task 12 |
| Audio file in folder but not in Plex | Plex refresh is best-effort; wait for Plex's own scan, or check library type is `Music` and token is valid |
| Episodes never appear / always re-aired | Check `data\state.db` `episodes` table is being written (record happens after filing) |
