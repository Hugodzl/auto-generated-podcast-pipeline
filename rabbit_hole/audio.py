import os
import shutil
import subprocess
import uuid
from pathlib import Path

_PROBE_TIMEOUT = 30      # seconds for the ffprobe bitrate read
_ENCODE_TIMEOUT = 600    # seconds for one re-encode (ample for a 90 MB / ~40 min file)
_DEFAULT_BITRATE = "192k"


class AudioBoostError(RuntimeError):
    pass


def _source_bitrate(path: Path, ffprobe_command: str) -> str:
    """Overall container bitrate as an ffmpeg -b:a value (e.g. '256k'). Falls back
    to the default if it can't be read, so we never silently downgrade quality."""
    try:
        result = subprocess.run(
            [ffprobe_command, "-v", "error", "-show_entries", "format=bit_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _DEFAULT_BITRATE
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw.isdigit():
        return _DEFAULT_BITRATE
    default_kbps = int(_DEFAULT_BITRATE.rstrip("k"))
    kbps = max(default_kbps, round(int(raw) / 1000))  # never re-encode below the baseline quality
    return f"{kbps}k"


def boost_loudness(path: str | Path, *, gain: float = 1.4, peak_ceiling_db: float = -1.0,
                   ffmpeg_command: str = "ffmpeg",
                   ffprobe_command: str = "ffprobe") -> None:
    """Re-encode `path` in place, louder by `gain`x with a true-peak ceiling.

    `volume=<gain>` does the loudness work; `alimiter` catches only peaks that
    would breach `peak_ceiling_db`, so nothing clips. Encodes to a sibling temp
    file and atomically replaces the original. On any ffmpeg/ffprobe failure the
    original is left untouched and AudioBoostError is raised.
    """
    path = Path(path)
    if not path.exists():
        raise AudioBoostError(f"audio file not found: {path}")

    limit = 10 ** (peak_ceiling_db / 20)              # -1 dBFS -> 0.8913 linear
    bitrate = _source_bitrate(path, ffprobe_command)
    tmp = path.with_name(f"{path.stem}.boost.{uuid.uuid4().hex[:8]}.tmp{path.suffix}")  # unique temp; keep .m4a so ffmpeg infers the muxer

    cmd = [
        ffmpeg_command, "-y", "-loglevel", "error", "-i", str(path),
        "-af", f"volume={gain},alimiter=limit={limit:.4f}:level=false",
        "-c:a", "aac", "-b:a", bitrate, "-map_metadata", "0",
        str(tmp),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=_ENCODE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        tmp.unlink(missing_ok=True)
        raise AudioBoostError(f"ffmpeg failed for {path}: {e}") from e

    if result.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise AudioBoostError(
            f"ffmpeg returned {result.returncode} for {path}: {result.stderr.strip()}"
        )
    try:
        os.replace(tmp, path)   # atomic; on Windows can fail if Plex holds the file open
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise AudioBoostError(f"could not replace {path}: {e}") from e


def boost_folder(target_dir: str | Path, backup_dir: str | Path, *,
                 gain: float = 1.4) -> tuple[list[str], list[tuple[str, str]]]:
    """Back up and boost every .m4a in `target_dir`.

    Each file is copied to `backup_dir` (skipped if a backup already exists, so a
    re-run never clobbers the pristine original) and then boosted in place.
    Continues past individual failures. Returns (boosted_names, failed) where
    failed is a list of (name, error_message).
    """
    target_dir, backup_dir = Path(target_dir), Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    boosted, failed = [], []
    for f in sorted(target_dir.glob("*.m4a")):
        backup = backup_dir / f.name
        if not backup.exists():
            shutil.copy2(f, backup)
        try:
            boost_loudness(f, gain=gain)
            boosted.append(f.name)
        except AudioBoostError as e:
            failed.append((f.name, str(e)))
    return boosted, failed
