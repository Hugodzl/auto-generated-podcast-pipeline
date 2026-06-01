import shutil
import subprocess
from pathlib import Path

import pytest

from rabbit_hole.audio import AudioBoostError, boost_loudness

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe required for audio tests",
)


def _make_clip(path: Path, *, volume: float = 0.2, freq: int = 300, seconds: int = 1) -> None:
    """Synthesize a mono sine .m4a. `volume` scales the (low-amplitude) lavfi sine;
    a large value (e.g. 16) yields a near-full-scale 'hot' source."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
         "-af", f"volume={volume}", "-c:a", "aac", "-b:a", "192k", str(path)],
        check=True,
    )


def _detect(path: Path, key: str) -> float:
    """Return mean_volume / max_volume (dB) from ffmpeg volumedetect."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    for line in out.splitlines():
        if f"{key}:" in line:
            return float(line.split(f"{key}:")[1].strip().split()[0])
    raise AssertionError(f"no {key} in ffmpeg output:\n{out}")


def test_boost_raises_mean_by_about_2_9_db(tmp_path):
    clip = tmp_path / "quiet.m4a"
    _make_clip(clip, volume=0.2)
    before = _detect(clip, "mean_volume")
    boost_loudness(clip, gain=1.4)
    after = _detect(clip, "mean_volume")
    assert after - before == pytest.approx(2.9, abs=0.6)


def test_boost_never_clips_a_hot_source(tmp_path):
    clip = tmp_path / "hot.m4a"
    _make_clip(clip, volume=16)        # source already ~0 dBFS; x1.4 would clip without the limiter
    boost_loudness(clip, gain=1.4)
    # alimiter holds PCM at -1 dB; lossy AAC may overshoot a few tenths, but never clips.
    assert _detect(clip, "max_volume") <= -0.5


def test_boost_preserves_metadata(tmp_path):
    from mutagen import File as MutagenFile
    clip = tmp_path / "tagged.m4a"
    _make_clip(clip)
    audio = MutagenFile(clip, easy=True)
    audio["title"] = "Keep Me"
    audio["album"] = "The Random Rabbit Hole"
    audio.save()
    boost_loudness(clip, gain=1.4)
    after = MutagenFile(clip, easy=True)
    assert after["title"] == ["Keep Me"]
    assert after["album"] == ["The Random Rabbit Hole"]


def test_boost_missing_file_raises(tmp_path):
    with pytest.raises(AudioBoostError):
        boost_loudness(tmp_path / "nope.m4a")


def test_boost_bad_input_leaves_original_and_raises(tmp_path):
    junk = tmp_path / "bad.m4a"
    junk.write_bytes(b"not audio at all")
    with pytest.raises(AudioBoostError):
        boost_loudness(junk)
    assert junk.read_bytes() == b"not audio at all"        # original untouched
    assert not (tmp_path / "bad.boost.tmp.m4a").exists()   # temp cleaned up
