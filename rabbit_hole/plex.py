import re
import shutil
import time
from pathlib import Path

import httpx
from plexapi.server import PlexServer

from rabbit_hole.config import PlexConfig
from rabbit_hole.notebooklm import GeneratedEpisode

ILLEGAL = re.compile(r'[\\/:*?"<>|]+')

# Album identity. Plex's tv.plex.agents.music agent mints a NEW album per
# scanned track, so these tags alone do NOT keep episodes together (confirmed
# live: two tracks sharing album + albumartist + year — even a shared
# MusicBrainz Album Id — still landed in separate albums). _unify_albums merges
# same-named albums back into one after each scan, which is the only thing the
# agent respects.
ALBUM = "The Random Rabbit Hole"
ALBUM_ARTIST = "Hugo Déziel"

# Best-effort wait for a path-scoped scan to index a just-filed track before we
# unify albums. Bounded so a stuck scan can't hang a weekly run.
_SCAN_POLL_ATTEMPTS = 40
_SCAN_POLL_INTERVAL_S = 3


def sanitize(name: str) -> str:
    return ILLEGAL.sub("-", name).strip().strip("-").strip()


def _track_int(value) -> int:
    """First integer in an EasyMP4 tracknumber value (e.g. ['5'] or ['5/12']);
    0 for a missing/blank/non-numeric tag."""
    if not value:
        return 0
    raw = str(value[0]).split("/")[0].strip()
    return int(raw) if raw.isdigit() else 0


def next_track_number(target_dir: Path) -> int:
    """1 + the highest tracknumber already present among the album's .m4a files
    (1 if none). Derived from what's actually on disk, so it covers pipeline,
    rescue, and one-off (book) episodes alike and self-heals if a number drifts."""
    from mutagen import File as MutagenFile
    highest = 0
    for p in target_dir.glob("*.m4a"):
        audio = MutagenFile(p, easy=True)
        if audio is None:
            continue
        highest = max(highest, _track_int(audio.get("tracknumber")))
    return highest + 1


def episode_filename(ep: GeneratedEpisode, topic_bucket: str, existing: set[str]) -> str:
    ext = ep.audio_path.suffix.lower()
    topic = sanitize(topic_bucket) or "Misc"
    title = sanitize(ep.paper.title) or sanitize(ep.paper.doi or "") or "untitled"
    base = f"{topic} - {title}"
    candidate = f"{base}{ext}"
    n = 2
    while candidate in existing:
        candidate = f"{base} ({n}){ext}"
        n += 1
    return candidate


def tag_audio(path: Path, ep: GeneratedEpisode, topic_bucket: str) -> None:
    from mutagen import File as MutagenFile
    audio = MutagenFile(path, easy=True)
    if audio is None:
        return
    audio["artist"] = ALBUM_ARTIST
    audio["albumartist"] = ALBUM_ARTIST  # necessary but not sufficient (see _unify_albums)
    audio["album"] = ALBUM
    audio["title"] = ep.paper.title[:200]
    audio["genre"] = topic_bucket
    audio["date"] = str(ep.paper.published_at.year)
    audio.save()


def file_episode(ep: GeneratedEpisode, topic_bucket: str, config: PlexConfig,
                 token: str) -> Path:
    target_dir = Path(config.target_folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.name for p in target_dir.iterdir()}
    filename = episode_filename(ep, topic_bucket, existing)
    dest = target_dir / filename
    shutil.move(str(ep.audio_path), str(dest))
    tag_audio(dest, ep, topic_bucket)
    try:
        _refresh_plex(config, token)
    except Exception:  # noqa: BLE001 - refresh is best-effort; Plex rescans on its own schedule
        pass
    return dest


def _refresh_plex(config: PlexConfig, token: str) -> None:
    server = PlexServer(config.base_url, token)
    section = server.library.section(config.library_name)
    section.update()  # path-scoped refresh of the section
    _wait_for_scan(section)
    _unify_albums(section, server)


def _wait_for_scan(section) -> None:
    """Wait (bounded) until the section finishes scanning, so a just-filed track
    is indexed before we unify albums."""
    time.sleep(_SCAN_POLL_INTERVAL_S)
    for _ in range(_SCAN_POLL_ATTEMPTS):
        section.reload()
        if not section.refreshing:
            return
        time.sleep(_SCAN_POLL_INTERVAL_S)


def _unify_albums(section, server) -> None:
    """Fold every album matching ALBUM / ALBUM_ARTIST into the largest/oldest one.

    Plex's online music agent gives each newly-scanned track its own album, so
    without this each weekly episode would spawn a fresh duplicate album. Merging
    is idempotent and self-healing: it runs every refresh and re-absorbs any
    stray album, so a track missed on one run (not yet indexed) is caught on the
    next.
    """
    albums = [a for a in section.albums()
              if a.title == ALBUM and a.parentTitle == ALBUM_ARTIST]
    if len(albums) < 2:
        return
    albums.sort(key=lambda a: (-len(a.tracks()), a.ratingKey))
    winner, losers = albums[0], albums[1:]
    ids = ",".join(str(a.ratingKey) for a in losers)
    server.query(f"/library/metadata/{winner.ratingKey}/merge?ids={ids}",
                 method=server._session.put)
