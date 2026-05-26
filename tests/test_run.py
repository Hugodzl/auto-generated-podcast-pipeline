from datetime import datetime, timezone
from pathlib import Path

import pytest

from rabbit_hole.config import Config
from rabbit_hole.notebooklm import GeneratedEpisode, NotebookLMError
from rabbit_hole.resolver import Paper
from rabbit_hole.run import run_pipeline
from rabbit_hole.sources import FeedItem
from rabbit_hole.state import State, hash_str

MIGRATIONS = Path(__file__).parent.parent / "migrations"


class _NullLog:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _config(episodes_per_run=1):
    return Config.model_validate({
        "schedule": {"cron": "0 7 * * MON", "episodes_per_run": episodes_per_run},
        "ranking": {"recency_half_life_days": 30},
        "topics": [{"name": "Animals", "weight": 1.0, "keywords": ["whale"]}],
        "sources": [{"name": "Nature", "type": "rss", "url": "http://x", "weight": 1.0}],
        "notebooklm": {"nlm_command": "nlm"},
        "plex": {"base_url": "http://127.0.0.1:32400/", "library_name": "L",
                 "target_folder": "ignored"},
        "apis": {"unpaywall_email": "e@x.com"},
        "notifications": {"discord_webhook_keyring_key": "k"},
    })


def _make_paper(title, doi):
    pub = datetime.now(timezone.utc)
    item = FeedItem(source_name="Nature", source_weight=1.0, title=title,
                    summary="whale communication", link=f"http://x/{doi}",
                    published_at=pub, raw={})
    return Paper(doi=doi, title=title, authors=[], abstract="whale communication",
                 published_at=pub, pdf_path=Path("p.pdf"), language="en", source=item)


def test_run_pipeline_files_episode(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config()

    paper = _make_paper("Whale song", "10.1/x")
    ep = GeneratedEpisode(paper=paper, audio_path=tmp_path / "a.m4a",
                          duration_seconds=1200, nlm_notebook_id="n1")

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s: iter([paper.source]))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda *a, **k: paper)
    monkeypatch.setattr("rabbit_hole.run.generate", lambda *a, **k: ep)
    filed = []
    monkeypatch.setattr("rabbit_hole.run.file_episode",
                        lambda e, bucket, plexcfg, token: (filed.append((e, bucket)) or (tmp_path / "f.m4a")))
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 1
    assert len(filed) == 1
    assert filed[0][1] == "Animals"  # topic_bucket passed through
    assert st.has_episode(doi="10.1/x", title_hash=hash_str("Whale song")) is True


def test_run_pipeline_skips_already_published(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config()
    paper = _make_paper("Whale song", "10.1/x")
    # pre-record the episode so it should be skipped
    st.record_episode(doi="10.1/x", title_hash=hash_str("Whale song"), title="Whale song",
                      language="en", source_name="Nature", topic_bucket="Animals",
                      score=1.0, pdf_path="p.pdf", audio_path="a.m4a")

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s: iter([paper.source]))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda *a, **k: paper)
    def _should_not_be_called(*a, **k):
        raise AssertionError("generate should not run for an already-published paper")
    monkeypatch.setattr("rabbit_hole.run.generate", _should_not_be_called)
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 0


def test_run_pipeline_aborts_after_5_failures(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config(episodes_per_run=5)

    papers = [_make_paper(f"Whale paper {i}", f"10.1/{i}") for i in range(5)]
    items = [p.source for p in papers]
    by_link = {p.source.link: p for p in papers}

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s: iter(items))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda item, *a, **k: by_link[item.link])
    monkeypatch.setattr("rabbit_hole.run.generate",
                        lambda *a, **k: (_ for _ in ()).throw(NotebookLMError("boom")))
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    with pytest.raises(NotebookLMError):
        run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())


def test_run_pipeline_does_not_abort_on_nonconsecutive_failures(tmp_path, monkeypatch):
    # 8 papers, 6 failures total but never 5 in a row -> must complete, not abort.
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config(episodes_per_run=8)

    papers = [_make_paper(f"Whale paper {i}", f"10.1/{i}") for i in range(8)]
    items = [p.source for p in papers]
    by_link = {p.source.link: p for p in papers}

    # Pattern F F S F F S F F : 6 failures, 2 successes, max 2 consecutive failures.
    outcomes = iter([False, False, True, False, False, True, False, False])

    def fake_generate(paper, cfg_, out_dir):
        if next(outcomes):
            return GeneratedEpisode(paper=paper, audio_path=tmp_path / "a.m4a",
                                    duration_seconds=1, nlm_notebook_id="n")
        raise NotebookLMError("boom")

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s: iter(items))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda item, *a, **k: by_link[item.link])
    monkeypatch.setattr("rabbit_hole.run.generate", fake_generate)
    monkeypatch.setattr("rabbit_hole.run.file_episode", lambda e, b, p, t: tmp_path / "f.m4a")
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 2  # the two successes were filed; run did not abort
