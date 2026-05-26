from datetime import datetime, timezone
from pathlib import Path

import pytest

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
