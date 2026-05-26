import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rabbit_hole import notebooklm
from rabbit_hole.config import NotebookLMConfig
from rabbit_hole.notebooklm import GeneratedEpisode, generate, NotebookLMError
from rabbit_hole.resolver import Paper
from rabbit_hole.sources import FeedItem


def make_paper(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    item = FeedItem(source_name="Nature", source_weight=1.0, title="T",
                    summary="s", link="http://x",
                    published_at=datetime(2026, 5, 19, tzinfo=timezone.utc), raw={})
    return Paper(doi="10.1/x", title="T", authors=[], abstract="s",
                 published_at=item.published_at, pdf_path=pdf, language="en", source=item)


def test_generate_success(tmp_path, monkeypatch):
    paper = make_paper(tmp_path)
    audio = tmp_path / "out.m4a"
    audio.write_bytes(b"fake audio")

    def fake_run_nlm(cmd, pdf_path, out_dir):
        return audio  # pretend nlm produced this file

    monkeypatch.setattr("rabbit_hole.notebooklm._run_nlm", fake_run_nlm)
    ep = generate(paper, NotebookLMConfig(nlm_command="nlm"), out_dir=tmp_path)
    assert isinstance(ep, GeneratedEpisode)
    assert ep.audio_path == audio
    assert ep.paper is paper


def test_generate_raises_when_no_output(tmp_path, monkeypatch):
    paper = make_paper(tmp_path)
    monkeypatch.setattr("rabbit_hole.notebooklm._run_nlm", lambda *a, **k: None)
    with pytest.raises(NotebookLMError):
        generate(paper, NotebookLMConfig(nlm_command="nlm"), out_dir=tmp_path)


def test_run_nlm_returns_new_audio_file(tmp_path, monkeypatch):
    from rabbit_hole import notebooklm
    out = tmp_path / "audio"

    def fake_run(cmd, capture_output, text, timeout):
        out.mkdir(parents=True, exist_ok=True)
        (out / "episode.m4a").write_bytes(b"audio")
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(notebooklm.subprocess, "run", fake_run)
    result = notebooklm._run_nlm("nlm", tmp_path / "p.pdf", out)
    assert result == out / "episode.m4a"


def test_run_nlm_none_on_nonzero_returncode(tmp_path, monkeypatch):
    from rabbit_hole import notebooklm

    def fake_run(cmd, capture_output, text, timeout):
        class R:
            returncode = 1
        return R()

    monkeypatch.setattr(notebooklm.subprocess, "run", fake_run)
    assert notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o") is None


def test_run_nlm_none_on_timeout(tmp_path, monkeypatch):
    import subprocess
    from rabbit_hole import notebooklm

    def fake_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(notebooklm.subprocess, "run", fake_run)
    assert notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o") is None


def test_run_nlm_none_when_command_missing(tmp_path, monkeypatch):
    from rabbit_hole import notebooklm

    def fake_run(cmd, capture_output, text, timeout):
        raise FileNotFoundError("nlm not on PATH")

    monkeypatch.setattr(notebooklm.subprocess, "run", fake_run)
    assert notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o") is None
