"""One-time (re-runnable) backfill: assign ascending track numbers + generation-date
tags to the episodes already in the Plex folder, oldest -> newest.

Track numbers make Plexamp sort the album deterministically (newest = highest); the
date tag becomes each episode's real generation day (from the state DB, or file mtime
for one-offs not in the DB) instead of the source paper's year. After writing tags it
refreshes Plex and, as a safety net, sets each track's index directly via the API so
the reorder applies even before a rescan re-reads the changed tags.

Usage:  uv run python scripts/number_existing_episodes.py
"""
import sys
from datetime import date
from pathlib import Path

import keyring
from mutagen import File as MutagenFile
from plexapi.server import PlexServer

from rabbit_hole.config import load_config
from rabbit_hole.plex import (ALBUM, ALBUM_ARTIST, _refresh_plex,
                              plan_episode_numbering)
from rabbit_hole.state import State

CONFIG_PATH = Path("config/config.toml")
DB_PATH = Path("data/state.db")
MIGRATIONS = Path("migrations")
KEYRING_SERVICE = "rabbit_hole"


def _write_tags(path: Path, track_number: int, generated_on: date) -> bool:
    audio = MutagenFile(path, easy=True)
    if audio is None:
        return False
    audio["tracknumber"] = str(track_number)
    audio["date"] = generated_on.isoformat()
    audio.save()
    return True


def _set_plex_indexes(plex_cfg, token: str, numbers: dict[str, int]) -> None:
    """Best-effort: set each matching track's index directly so the order applies
    immediately. `numbers` maps file basename -> track number. Per-track failures are
    non-fatal — the embedded tag + rescan is the durable mechanism."""
    server = PlexServer(plex_cfg.base_url, token)
    section = server.library.section(plex_cfg.library_name)
    for album in section.albums():
        if not (album.title == ALBUM and album.parentTitle == ALBUM_ARTIST):
            continue
        for track in album.tracks():
            try:
                fname = Path(track.media[0].parts[0].file).name
            except (IndexError, AttributeError):
                continue
            n = numbers.get(fname)
            if n is None:
                continue
            try:
                track.edit(**{"index.value": n, "index.locked": 1})
            except Exception as e:  # noqa: BLE001 - tag+rescan is the durable path
                print(f"  index edit FAILED for {fname}: {e}")


def main() -> int:
    cfg = load_config(CONFIG_PATH)
    target = Path(cfg.plex.target_folder)

    state = State(DB_PATH, MIGRATIONS)
    state.init()
    db_times = {Path(p).name: ts
                for p, ts in state.generated_at_by_audio_path().items()}

    files = [f.name for f in target.glob("*.m4a")]
    if not files:
        print(f"No .m4a files in {target}")
        return 0
    mtimes = {f: (target / f).stat().st_mtime for f in files}

    plan = plan_episode_numbering(files, mtimes, db_times)
    numbers: dict[str, int] = {}
    for name, number, gen_date in plan:
        ok = _write_tags(target / name, number, gen_date)
        numbers[name] = number
        flag = "" if ok else "   (NOT audio - tag skipped)"
        print(f"  {number:>3}  {gen_date}  {name}{flag}")
    print(f"\nNumbered {len(plan)} episodes (oldest=1, newest={len(plan)}).")

    token = keyring.get_password(KEYRING_SERVICE, "plex_token") or ""
    if not token:
        print("No plex_token in keyring; skipped Plex refresh + index edit.")
        return 0
    try:
        _refresh_plex(cfg.plex, token)
        print("Plex refreshed.")
    except Exception as e:  # noqa: BLE001 - refresh is best-effort
        print(f"Plex refresh failed (non-fatal): {e}")
    try:
        _set_plex_indexes(cfg.plex, token, numbers)
        print("Plex track indexes set.")
    except Exception as e:  # noqa: BLE001 - index edit is the safety net, not load-bearing
        print(f"Plex index edit failed (non-fatal): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
