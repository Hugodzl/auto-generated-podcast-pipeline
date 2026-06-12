import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

from rabbit_hole.config import NotebookLMConfig
from rabbit_hole.resolver import Paper

_CALL_TIMEOUT = 120            # seconds for a single non-waiting nlm call
_SOURCE_WAIT_TIMEOUT = 600     # seconds to wait for PDF ingest (`source add --wait`)
_AUDIO_POLL_INTERVAL = 15      # seconds between studio-artifact status polls
_AUDIO_POLL_TIMEOUT = 1800     # max seconds to wait for audio generation
_DOWNLOAD_RETRY_INTERVAL = 30  # seconds between download attempts
_DOWNLOAD_MAX_ATTEMPTS = 30    # ~15 min total: ride out the status->rendition lag

_log = structlog.get_logger()


class NotebookLMError(RuntimeError):
    pass


@dataclass
class GeneratedEpisode:
    paper: Paper
    audio_path: Path
    duration_seconds: int
    nlm_notebook_id: str


def _nlm(nlm_command: str, args: list[str], timeout: float):
    """Run one nlm subcommand. Returns the CompletedProcess, or None if the
    binary is missing or the call times out."""
    try:
        return subprocess.run(
            [nlm_command, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _nlm_json(nlm_command: str, args: list[str], timeout: float):
    """Run an nlm subcommand expected to emit JSON on success; return the parsed
    value, or None on any failure."""
    result = _nlm(nlm_command, args, timeout)
    if result is None or result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        return None


def _find_notebook_id(nlm_command: str, title: str) -> str | None:
    notebooks = _nlm_json(nlm_command, ["list", "notebooks", "--json"], _CALL_TIMEOUT)
    if not isinstance(notebooks, list):
        return None
    matches = [n for n in notebooks if n.get("title") == title and n.get("id")]
    matches.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    return matches[0]["id"] if matches else None


def _wait_for_audio(nlm_command: str, notebook_id: str) -> str | None:
    """Poll studio artifacts until the audio overview is completed. Returns the
    audio artifact id, or None if it does not complete before the timeout."""
    started = time.monotonic()
    deadline = started + _AUDIO_POLL_TIMEOUT
    polls = 0
    while True:
        artifacts = _nlm_json(
            nlm_command, ["status", "artifacts", notebook_id, "--json"], _CALL_TIMEOUT
        )
        if isinstance(artifacts, list):
            for art in artifacts:
                if art.get("type") == "audio" and art.get("status") == "completed":
                    return art.get("id")
        polls += 1
        if polls % 4 == 0:  # one tick per minute at the 15s interval
            _log.info("nlm_waiting", notebook_id=notebook_id,
                      elapsed_s=round(time.monotonic() - started))
        if time.monotonic() >= deadline:
            return None
        time.sleep(_AUDIO_POLL_INTERVAL)


def _download_audio(nlm_command: str, notebook_id: str, artifact_id: str,
                    out_path: Path) -> bool:
    """Download the audio overview, retrying until the rendition is served.

    nlm normalizes the studio status to 'completed' as soon as the artifact
    reaches status code 2, but the media files 404 until it reaches code 3 a few
    minutes later. `status artifacts --json` can't distinguish the two, so
    _wait_for_audio returns early; retry the download until an attempt produces
    the file (or the attempt budget is exhausted)."""
    for attempt in range(_DOWNLOAD_MAX_ATTEMPTS):
        result = _nlm(
            nlm_command,
            ["download", "audio", notebook_id, "--id", artifact_id,
             "--output", str(out_path), "--no-progress"],
            _CALL_TIMEOUT,
        )
        if result is not None and result.returncode == 0 and out_path.exists():
            return True
        if attempt < _DOWNLOAD_MAX_ATTEMPTS - 1:
            _log.warning("nlm_download_retry", notebook_id=notebook_id,
                         attempt=attempt + 1)
            time.sleep(_DOWNLOAD_RETRY_INTERVAL)
    return False


def _run_nlm(nlm_command: str, pdf_path: Path, out_dir: Path, *,
             audio_format: str = "deep_dive", audio_length: str = "default",
             focus: str = "") -> Path | None:
    """Generate an audio overview for one PDF via the nlm CLI.

    Multi-step: create a notebook, add the PDF as a source (waiting for ingest),
    request an audio overview, poll until it completes, then download the .m4a.
    The per-run notebook is deleted afterward. Any failure — non-zero exit,
    timeout, missing binary, or generation never completing — returns None so
    the orchestrator's failure budget handles them uniformly.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    title = f"rabbit-hole {pdf_path.stem} {uuid.uuid4().hex[:8]}"
    notebook_id: str | None = None
    try:
        created = _nlm(nlm_command, ["notebook", "create", title], _CALL_TIMEOUT)
        if created is None or created.returncode != 0:
            return None
        notebook_id = _find_notebook_id(nlm_command, title)
        if not notebook_id:
            return None
        _log.info("nlm_notebook_created", notebook_id=notebook_id, pdf=pdf_path.name)

        added = _nlm(
            nlm_command,
            ["source", "add", notebook_id, "--file", str(pdf_path),
             "--wait", "--wait-timeout", str(_SOURCE_WAIT_TIMEOUT)],
            _SOURCE_WAIT_TIMEOUT + _CALL_TIMEOUT,
        )
        if added is None or added.returncode != 0:
            return None
        _log.info("nlm_source_added", notebook_id=notebook_id)

        audio_args = ["audio", "create", notebook_id,
                      "--format", audio_format, "--length", audio_length]
        if focus:
            audio_args += ["--focus", focus]
        audio_args.append("--confirm")
        requested = _nlm(nlm_command, audio_args, _CALL_TIMEOUT)
        if requested is None or requested.returncode != 0:
            return None
        _log.info("nlm_audio_requested", notebook_id=notebook_id)

        artifact_id = _wait_for_audio(nlm_command, notebook_id)
        if not artifact_id:
            return None
        _log.info("nlm_audio_ready", notebook_id=notebook_id, artifact_id=artifact_id)

        out_path = out_dir / f"{notebook_id}_audio.m4a"
        if _download_audio(nlm_command, notebook_id, artifact_id, out_path):
            return out_path
        return None
    finally:
        if notebook_id:
            _nlm(nlm_command, ["notebook", "delete", notebook_id, "--confirm"],
                 _CALL_TIMEOUT)
            _log.info("nlm_notebook_deleted", notebook_id=notebook_id)


def generate(paper: Paper, config: NotebookLMConfig, out_dir: Path, *,
             audio_format: str | None = None, audio_length: str | None = None,
             focus: str | None = None) -> GeneratedEpisode:
    fmt = audio_format if audio_format is not None else config.audio_format
    length = audio_length if audio_length is not None else config.audio_length
    foc = focus if focus is not None else config.focus
    audio = _run_nlm(config.nlm_command, paper.pdf_path, Path(out_dir),
                     audio_format=fmt, audio_length=length, focus=foc)
    if audio is None or not audio.exists():
        raise NotebookLMError(f"nlm produced no audio for {paper.title!r}")
    return GeneratedEpisode(
        paper=paper,
        audio_path=audio,
        duration_seconds=0,   # populated by the plex tagging step via mutagen
        nlm_notebook_id="",   # the per-run notebook is deleted after download
    )
