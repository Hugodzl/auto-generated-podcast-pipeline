# The Random Rabbit Hole

Every Monday at 7am a Windows box wakes up, reads the week's science, picks the
two papers most worth hearing about, has NotebookLM narrate them, and leaves the
episodes in my Plex library as a properly tagged album. I listen to them on the
drive.

Its last seven scheduled runs have all completed cleanly and unattended — two
episodes each, about twenty minutes a run, no one watching.

This repo is the whole thing: the feed reader, the open-access PDF hunt, the
ranking, the CLI driving, the crash recovery, and the Plex filing. ~2,300 lines
of Python, ~3,700 lines of tests.

---

## What it actually produces

The last few episodes it chose for itself, unprompted:

| Topic | Episode | Source |
|---|---|---|
| AI studies | Hyperbolic Inverse Reinforcement Learning | arXiv cs.LG |
| Philosophy | Thomas Hill Green | Stanford Encyclopedia of Philosophy |
| AI studies | Learning to Predict, Discover, and Reason in High-Cardinality Event Sequences | arXiv cs.AI |
| Technology and society | High prevalence of antagonizing rhetoric in trending topics on Twitter/X | arXiv cs.CL |
| Animal behavior and cognition | Why is This Octopus Hitching a Ride on a Dolphin? | Nautilus |

Twenty-four episodes so far, across five topics. Each one lands as an `.m4a` with
artist, album, genre, date and an ascending track number, so Plex shows one album
that grows a track a week rather than a pile of loose files.

---

## How it works

```
  20 RSS/Atom feeds           Nature, Science, PNAS, arXiv, bioRxiv,
          |                   Aeon, Noema, Nautilus, SEP, ...
          v
  poll ---------------------  conditional GET (ETag/Last-Modified), per-source
          |                   health tracking, dedup against everything ever seen
          v
  rank ---------------------  source weight x topic match x 30-day recency decay,
          |                   then interleaved so topics take turns
          v
  resolve ------------------  find an open-access PDF: Unpaywall -> arXiv ->
          |                   bioRxiv/medRxiv -> Semantic Scholar -> HAL.
          |                   Essay feeds skip this entirely (no PDF exists).
          v
  generate -----------------  drive the NotebookLM CLI: create notebook, add
          |                   source, request an audio overview, poll, download,
          |                   delete the notebook
          v
  boost --------------------  ffmpeg +40% with a -1 dBFS true-peak limiter
          |
          v
  file ---------------------  tag, move into the Plex folder, trigger a scan,
          |                   merge the album Plex insists on splitting
          v
  notify -------------------  Discord summary: what landed, what stopped it,
                              which feeds have been failing
```

State lives in SQLite with forward-only numbered migrations. Secrets live in
Windows Credential Manager — there is not a single credential in this repo or in
the config file.

---

## Decisions worth defending

This is the part I'd actually want to talk about.

### Matching keywords is not the same as knowing what a paper is about

Episode 8 shipped as **"Human relationships and psychology — An astro-neural-field
model with application to cortical spreading depolarization."** It is a paper about
potassium dynamics in brain tissue. It matched the relationships topic because the
abstract contains the word *coupled*, and `"couple" in text` is `True`.

The same bug was waiting in `"tool use"` inside *tool used*, and `"alignment"`
inside *sequence alignment*. The fix is whole-word matching with exactly one
optional plural suffix — enough that `whale` still matches *whales*, not enough
that `couple` reaches *coupled*.

Fixing the matcher exposed a second, subtler problem: the scoring curve was
`1 + log1p(hits)`, which gave a single incidental keyword **61% of the value of
five**. One stray "neural network" in a pollinator paper outscored a genuine AI
paper from a lower-weighted feed. Dropping the `1 +` puts one hit at 0.69 against
1.79 for five, so being *about* a topic finally beats mentioning it.

Both are pinned by regression tests that load the real shipped config rather than
a fixture — the bug was never in the code, it was in the rules, and a fixture
wouldn't have caught either one.

### Lowering a source's weight does not fix diversity

arXiv cs.AI/LG/CL are **11,100 of the 18,616 items this pipeline has ever
ingested** — just under 60% — and their abstracts hit four to six AI keywords
each. They took the first seven episodes. All of them.

Dropping their weight from 0.7 to 0.6 was the obvious move and it was the wrong
one: weights change *which* item wins, not the fact that the same topic keeps
winning. The actual fix is structural — rank into per-topic pools, then interleave
so each cycle takes every topic's best item. The weight change stayed, but only as
a tiebreaker.

### A spent quota is not five bad papers

NotebookLM caps audio overviews per account per day. When the cap hit mid-run, it
surfaced as a generic non-zero exit — indistinguishable from a paper that simply
failed to generate. So the run spent its entire failure budget retrying papers that
could never have worked, then announced **"run failed"** to Discord even though two
episodes had already been filed in Plex.

Three changes, each addressing a different layer of that:

- A typed `NotebookLMRateLimited` exception that ends the walk instead of retrying,
  because no other paper would fare better.
- A `partial` run status, so a run that achieved something doesn't report as a
  failure. Waking up to "failed" when it worked is the wrong thing to wake up to.
- Every failing CLI step now logs what the CLI actually said. The orchestrator had
  only ever seen `"nlm produced no audio"`, which hid the real cause for a week.

### "Completed" did not mean the file existed

The CLI normalizes a studio artifact to `completed` as soon as it reaches internal
status code 2 — but the media files 404 until code 3, several minutes later, and
`status artifacts --json` cannot distinguish the two.

There is no flag to wait for. The honest fix is to stop trusting the status and
retry the download on a budget (30 attempts, 30 seconds apart) until an attempt
actually produces a file.

### Don't predict what a remote service will accept — ask it

The essay and encyclopedia feeds (Aeon, Noema, Nautilus, SEP) have no DOI and no
open-access PDF, so NotebookLM reads the article page directly. Which raised the
question of how to know, before committing to a run, whether it *will*.

HTTP status fails as a predictor. robots.txt fails as a predictor. Both fail **in
both directions**: sites that 403 a plain client ingest fine, and sites that
cheerfully return 200 get refused. And a refusal is expensive — one bad URL aborts
the notebook after every source ahead of it has already ingested.

So the preflight doesn't guess. It runs `nlm source add` against each URL and
believes the answer.

### Plex ignores your tags

The online music agent mints a **new album per scanned track**. Matching `album`,
matching `albumartist`, matching year, even a shared MusicBrainz Album ID —
confirmed live, still two separate albums.

The only operation the agent respects is an explicit merge. So after every scan the
pipeline folds same-named albums into the largest one. It's idempotent and
self-healing by design: a track that wasn't indexed in time gets absorbed on the
next run rather than stranded forever.

### A killed run leaves finished work on the server

When run 5 was killed mid-episode, the audio for the episode in flight had already
been generated server-side. It was paid for and complete, sitting in a notebook
nobody owned.

Startup now does adopt-or-delete. Any `rabbit-hole …` notebook whose title matches
a pending-generation row gets its audio downloaded and counted toward the current
run's target; everything else under that prefix is deleted. Crash recovery that
*finishes* the interrupted work instead of just cleaning up after it.

---

## When it broke

Fifteen runs since May. Three failed, and each one changed the design:

**Run 5** — I launched a multi-hour job as a child of an interactive session and
closed the session. The whole process tree went with it, mid-episode. Runs are now
launched detached via a Scheduled Task, and the orphaned audio from that run is
what motivated the recovery pass above.

**Run 6** — a single paper failed to generate and took the run with it. Led to the
consecutive-failure budget: five *in a row* ends the run, one doesn't, and the
counter resets on every success.

**Run 8** — the quota incident described above. Two episodes landed; the run
reported failure anyway.

Runs 9 through 15 have all completed cleanly. The failures are listed here because
they're the most honest thing in this README: this is software that has actually
been operated, and every one of those three left a fix behind.

---

## Engineering notes

- **227 tests**, 3,728 lines of test against 2,298 lines of source. Network is
  mocked at the HTTP transport layer (`respx`), not by stubbing the project's own
  functions, so the tests exercise the real request-building and retry logic.
- **Forward-only SQL migrations**, applied by version number at startup.
- **An OS-level lockfile** (`msvcrt.locking`) rather than a PID file: the OS
  releases it when the handle closes *or* the process dies, so a crashed run can
  never leave a stale lock behind. A contention skip exits 0 on purpose — Task
  Scheduler would otherwise mark the weekly job failed and retry it.
- **Structured logging** (`structlog`) with a Discord webhook for the weekly
  summary, including a warning for any feed that has failed several runs in a row.
- **Polite API citizenship**: per-provider throttles with a safety-net default,
  `Retry-After` honored on 429, and a 30-day response cache — a cold run would
  otherwise fire well over a thousand lookups.
- **No credentials anywhere.** Plex token, Discord webhook and the optional
  Semantic Scholar key are read by name from Windows Credential Manager. The
  outbound `User-Agent` contact address comes from config, enforced by a test that
  fails if a literal `mailto:` ever reappears in the source.

### Rough edges I know about

The lockfile is Windows-only (`msvcrt`); a port would need an `fcntl` branch. And
`--length long` turns out to be a ceiling rather than a target — the same settings
produced 46 minutes from one PDF and 22 minutes from twenty-two sources, because
source count is the real lever. Fewer sources and a tighter focus brief make a
longer episode, which is the opposite of what I assumed when I built the one-off
tooling.

---

## Running it yourself

You'll need Python 3.11+, [uv](https://docs.astral.sh/uv/), ffmpeg, a Plex server,
and a Google account for NotebookLM.

```
uv sync
npm install -g notebooklm-mcp-cli && nlm login
copy config\config.example.toml config\config.toml   # then edit topics/sources
uv run python -m rabbit_hole.setup                   # stores Plex token + webhook
uv run python -m rabbit_hole.run                     # dry run with episodes_per_run = 1
```

**[docs/INTEGRATION.md](docs/INTEGRATION.md)** is the real setup guide — first-contact
CLI discovery, credentials, the Plex library settings that actually matter, the
smoke test, scheduling, and troubleshooting.

Once it works, schedule it and walk away:

```
powershell -File scripts\register_weekly_task.ps1
```

Never launch a run as a child of a terminal or an SSH session you might close. See
run 5.

### One-off episodes

Separate from the weekly run: build a single episode from a curated reading list or
a book PDF, bypassing poll/resolve/rank entirely. An episode is a small
`EpisodeSpec` — title, Plex bucket, sources, focus brief — and all the machinery
lives in `rabbit_hole/oneoff.py`.

```
uv run python scripts\make_topic_episode.py --fetch --check
powershell -ExecutionPolicy Bypass -File scripts\run_oneoff_detached.ps1 topic
```

Always `--check` first. It verifies the PDFs, ffmpeg, the Plex token, that the CLI
login is still valid, and asks NotebookLM itself whether it will ingest each URL.

---

## License

MIT. See [LICENSE](LICENSE).
