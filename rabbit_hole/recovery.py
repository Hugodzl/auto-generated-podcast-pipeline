"""Startup recovery for orphaned NotebookLM notebooks.

A killed run can leave per-episode notebooks behind, often with the audio fully
generated server-side (observed live on run 5). Adopt-or-delete: a completed
artifact whose notebook matches a pending_generations snapshot (the notebook
title embeds the PDF stem) is downloaded and handed back to the orchestrator as
a ready episode; everything else rabbit-hole-prefixed is deleted.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rabbit_hole.notebooklm import (
    _CALL_TIMEOUT,
    GeneratedEpisode,
    _download_audio,
    _nlm,
    _nlm_json,
)
from rabbit_hole.resolver import Paper
from rabbit_hole.sources import FeedItem
from rabbit_hole.state import State

_PREFIX = "rabbit-hole "


@dataclass
class RecoveredEpisode:
    episode: GeneratedEpisode
    topic_bucket: str
    score: float
    pending_id: int


def _delete_notebook(nlm_command: str, notebook_id: str) -> None:
    _nlm(nlm_command, ["notebook", "delete", notebook_id, "--confirm"], _CALL_TIMEOUT)


def _paper_from_pending(row: dict) -> Paper:
    item = FeedItem(
        source_name=row["source_name"], source_weight=row["source_weight"],
        title=row["title"], summary=row["summary"] or "", link=row["link"],
        published_at=datetime.fromisoformat(row["published_at"]),
    )
    return Paper(doi=row["doi"], title=row["title"], authors=[],
                 abstract=row["summary"] or "", published_at=item.published_at,
                 pdf_path=Path(row["pdf_path"]), language=row["language"],
                 source=item)


def _completed_audio_id(nlm_command: str, notebook_id: str) -> str | None:
    artifacts = _nlm_json(nlm_command, ["status", "artifacts", notebook_id, "--json"],
                          _CALL_TIMEOUT)
    for art in artifacts if isinstance(artifacts, list) else []:
        if art.get("type") == "audio" and art.get("status") == "completed":
            return art.get("id")
    return None


def recover_orphans(nlm_command: str, state: State, audio_dir: Path,
                    logger) -> list[RecoveredEpisode]:
    notebooks = _nlm_json(nlm_command, ["list", "notebooks", "--json"], _CALL_TIMEOUT)
    if not isinstance(notebooks, list):
        return []  # nlm unreachable: change nothing, retry next run
    pending_by_stem = {Path(row["pdf_path"]).stem: row for row in state.list_pending()}
    recovered: list[RecoveredEpisode] = []
    matched_ids: set[int] = set()

    for nb in notebooks:
        title, nb_id = nb.get("title") or "", nb.get("id")
        if not nb_id or not title.startswith(_PREFIX):
            continue
        parts = title.split()
        row = pending_by_stem.get(parts[1]) if len(parts) >= 2 else None
        if row is None:
            logger.warning("orphan_deleted", notebook_id=nb_id, title=title)
            _delete_notebook(nlm_command, nb_id)
            continue
        matched_ids.add(row["id"])
        art_id = _completed_audio_id(nlm_command, nb_id)
        if not art_id:
            logger.warning("orphan_unrecoverable", notebook_id=nb_id, title=row["title"])
            _delete_notebook(nlm_command, nb_id)
            state.delete_pending(row["id"])
            continue
        out_path = Path(audio_dir) / f"{nb_id}_audio.m4a"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not _download_audio(nlm_command, nb_id, art_id, out_path):
            logger.error("orphan_download_failed", notebook_id=nb_id, title=row["title"])
            continue  # keep notebook + snapshot; retry next run
        ep = GeneratedEpisode(paper=_paper_from_pending(row), audio_path=out_path,
                              duration_seconds=0, nlm_notebook_id=nb_id)
        recovered.append(RecoveredEpisode(ep, row["topic_bucket"], row["score"], row["id"]))
        logger.info("orphan_adopted", notebook_id=nb_id, title=row["title"])
        _delete_notebook(nlm_command, nb_id)

    for row in pending_by_stem.values():
        if row["id"] not in matched_ids:
            logger.warning("pending_purged", title=row["title"])
            state.delete_pending(row["id"])
    return recovered
