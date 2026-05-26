import re
import shutil
from pathlib import Path

import httpx
from plexapi.server import PlexServer

from rabbit_hole.config import PlexConfig
from rabbit_hole.notebooklm import GeneratedEpisode

ILLEGAL = re.compile(r'[\\/:*?"<>|]+')


def sanitize(name: str) -> str:
    return ILLEGAL.sub("-", name).strip().strip("-").strip()


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
    audio["artist"] = "Hugo Déziel"
    audio["album"] = "The Random Rabbit Hole"
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
