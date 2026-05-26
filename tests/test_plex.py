from datetime import datetime, timezone
from pathlib import Path

from rabbit_hole.notebooklm import GeneratedEpisode
from rabbit_hole.plex import episode_filename, sanitize
from rabbit_hole.resolver import Paper
from rabbit_hole.sources import FeedItem


def make_ep(tmp_path, title="Sperm whales have vowels"):
    audio = tmp_path / "tmp.m4a"
    audio.write_bytes(b"audio")
    item = FeedItem(source_name="Nature", source_weight=1.0, title=title, summary="s",
                    link="http://x", published_at=datetime(2026, 5, 19, tzinfo=timezone.utc), raw={})
    paper = Paper(doi="10.1/x", title=title, authors=[], abstract="s",
                  published_at=item.published_at, pdf_path=tmp_path / "p.pdf",
                  language="en", source=item)
    return GeneratedEpisode(paper=paper, audio_path=audio, duration_seconds=1200, nlm_notebook_id="n1")


def test_sanitize_strips_illegal_chars():
    assert sanitize('a/b:c*d?') == "a-b-c-d"


def test_episode_filename_uses_topic_not_date(tmp_path):
    ep = make_ep(tmp_path)
    name = episode_filename(ep, topic_bucket="Animal behavior", existing=set())
    assert name == "Animal behavior - Sperm whales have vowels.m4a"


def test_episode_filename_dedups_collision(tmp_path):
    ep = make_ep(tmp_path)
    existing = {"Animal behavior - Sperm whales have vowels.m4a"}
    name = episode_filename(ep, topic_bucket="Animal behavior", existing=existing)
    assert name == "Animal behavior - Sperm whales have vowels (2).m4a"


def test_episode_filename_falls_back_when_title_empty(tmp_path):
    # A title made entirely of illegal chars sanitizes to "" -> must fall back to the DOI
    ep = make_ep(tmp_path, title="///:*?")
    name = episode_filename(ep, topic_bucket="Animals", existing=set())
    assert name == "Animals - 10.1-x.m4a"
