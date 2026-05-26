import subprocess
from dataclasses import dataclass
from pathlib import Path

from rabbit_hole.config import NotebookLMConfig
from rabbit_hole.resolver import Paper

AUDIO_EXTS = (".m4a", ".wav", ".mp3", ".m4v")


class NotebookLMError(RuntimeError):
    pass


@dataclass
class GeneratedEpisode:
    paper: Paper
    audio_path: Path
    duration_seconds: int
    nlm_notebook_id: str


def _run_nlm(nlm_command: str, pdf_path: Path, out_dir: Path) -> Path | None:
    """Invoke the nlm CLI. Returns the produced audio file, or None on failure.

    NOTE: exact subcommands/flags are confirmed during manual integration
    (see README setup). This wraps the documented 'audio' generation flow.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    before = set(out_dir.iterdir())
    result = subprocess.run(
        [nlm_command, "audio", "--source", str(pdf_path), "--out", str(out_dir)],
        capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0:
        return None
    new_files = [p for p in out_dir.iterdir()
                 if p not in before and p.suffix.lower() in AUDIO_EXTS]
    return new_files[0] if new_files else None


def generate(paper: Paper, config: NotebookLMConfig, out_dir: Path) -> GeneratedEpisode:
    audio = _run_nlm(config.nlm_command, paper.pdf_path, Path(out_dir))
    if audio is None or not audio.exists():
        raise NotebookLMError(f"nlm produced no audio for {paper.title!r}")
    return GeneratedEpisode(
        paper=paper,
        audio_path=audio,
        duration_seconds=0,  # populated by plex tagging step via mutagen
        nlm_notebook_id="",
    )
