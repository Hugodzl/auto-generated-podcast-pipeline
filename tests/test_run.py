from datetime import datetime, timedelta, timezone
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


def _item(title, link, summary="whale communication", days_old=0, source_weight=1.0):
    pub = datetime.now(timezone.utc) - timedelta(days=days_old)
    return FeedItem(source_name="Nature", source_weight=source_weight, title=title,
                    summary=summary, link=link, published_at=pub, raw={})


def _paper_for(item, doi=None):
    return Paper(doi=doi, title=item.title, authors=[], abstract=item.summary,
                 published_at=item.published_at, pdf_path=Path("p.pdf"),
                 language="en", source=item)


def _ep(paper, tmp_path):
    return GeneratedEpisode(paper=paper, audio_path=tmp_path / "a.m4a",
                            duration_seconds=1, nlm_notebook_id="n")


@pytest.fixture(autouse=True)
def _noop_boost(monkeypatch):
    """Stub the ffmpeg loudness boost by default so pipeline tests exercise the
    happy path without shelling out (the synthetic ep.audio_path doesn't exist).
    Tests that assert on boost behavior re-patch rabbit_hole.run.boost_loudness."""
    monkeypatch.setattr("rabbit_hole.run.boost_loudness", lambda *a, **k: None)


def test_run_pipeline_files_episode(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config()
    item = _item("Whale song", "http://x/10.1234/whale")
    paper = _paper_for(item, doi="10.1234/whale")

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter([item]))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda *a, **k: paper)
    monkeypatch.setattr("rabbit_hole.run.generate", lambda *a, **k: _ep(paper, tmp_path))
    filed = []
    monkeypatch.setattr("rabbit_hole.run.file_episode",
                        lambda e, bucket, plexcfg, token: (filed.append((e, bucket)) or (tmp_path / "f.m4a")))
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 1
    assert len(filed) == 1
    assert filed[0][1] == "Animals"  # topic_bucket passed through
    assert st.has_episode(doi="10.1234/whale", title_hash=hash_str("Whale song")) is True


def test_run_pipeline_skips_already_published_before_resolving(tmp_path, monkeypatch):
    # Dedup happens pre-resolve: an already-published item must not even be resolved.
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config()
    item = _item("Whale song", "http://x/10.1234/whale")
    st.record_episode(doi="10.1234/whale", title_hash=hash_str("Whale song"), title="Whale song",
                      language="en", source_name="Nature", topic_bucket="Animals",
                      score=1.0, pdf_path="p.pdf", audio_path="a.m4a")

    def _no_resolve(*a, **k):
        raise AssertionError("resolve must not run for an already-published item")

    def _no_generate(*a, **k):
        raise AssertionError("generate must not run for an already-published item")

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter([item]))
    monkeypatch.setattr("rabbit_hole.run.resolve", _no_resolve)
    monkeypatch.setattr("rabbit_hole.run.generate", _no_generate)
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 0


def test_run_pipeline_aborts_after_5_consecutive_failures(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config(episodes_per_run=5)
    items = [_item(f"Whale paper {i}", f"http://x/p{i}") for i in range(5)]
    by_link = {it.link: _paper_for(it) for it in items}

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter(items))
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
    items = [_item(f"Whale paper {i}", f"http://x/p{i}") for i in range(8)]
    by_link = {it.link: _paper_for(it) for it in items}

    # Pattern F F S F F S F F : 6 failures, 2 successes, max 2 consecutive failures.
    outcomes = iter([False, False, True, False, False, True, False, False])

    def fake_generate(paper, cfg_, out_dir):
        if next(outcomes):
            return _ep(paper, tmp_path)
        raise NotebookLMError("boom")

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter(items))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda item, *a, **k: by_link[item.link])
    monkeypatch.setattr("rabbit_hole.run.generate", fake_generate)
    monkeypatch.setattr("rabbit_hole.run.file_episode", lambda e, b, p, t: tmp_path / "f.m4a")
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 2  # the two successes were filed; run did not abort


def test_run_pipeline_resolves_lazily_until_target_met(tmp_path, monkeypatch):
    # 10 eligible items, target 2 -> resolve is called only until 2 episodes are made.
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config(episodes_per_run=2)
    items = [_item(f"Whale paper {i}", f"http://x/p{i}") for i in range(10)]
    by_link = {it.link: _paper_for(it) for it in items}
    resolve_calls = []

    def fake_resolve(item, *a, **k):
        resolve_calls.append(item.link)
        return by_link[item.link]

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter(items))
    monkeypatch.setattr("rabbit_hole.run.resolve", fake_resolve)
    monkeypatch.setattr("rabbit_hole.run.generate", lambda paper, *a, **k: _ep(paper, tmp_path))
    monkeypatch.setattr("rabbit_hole.run.file_episode", lambda e, b, p, t: tmp_path / "f.m4a")
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 2
    assert len(resolve_calls) == 2  # stopped after 2; did not resolve the whole corpus


def test_run_pipeline_skips_resolve_misses_without_aborting(tmp_path, monkeypatch):
    # First 6 items fail to resolve (None); 6 > MAX_CONSECUTIVE_FAILURES(5).
    # Resolve misses must NOT count as nlm failures, so the run still completes.
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config(episodes_per_run=2)
    items = [_item(f"Whale paper {i}", f"http://x/p{i}") for i in range(8)]
    papers = {it.link: (None if i < 6 else _paper_for(it)) for i, it in enumerate(items)}

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter(items))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda item, *a, **k: papers[item.link])
    monkeypatch.setattr("rabbit_hole.run.generate", lambda paper, *a, **k: _ep(paper, tmp_path))
    monkeypatch.setattr("rabbit_hole.run.file_episode", lambda e, b, p, t: tmp_path / "f.m4a")
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 2  # no abort despite 6 consecutive resolve misses


def test_run_pipeline_files_in_descending_score_order(tmp_path, monkeypatch):
    # Selection order comes from rank (recency), not from poll order.
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config(episodes_per_run=2)
    fresh = _item("Whale fresh", "http://x/fresh", days_old=1)
    stale = _item("Whale stale", "http://x/stale", days_old=300)
    by_link = {fresh.link: _paper_for(fresh), stale.link: _paper_for(stale)}

    # poll yields stale first to prove ordering comes from rank, not poll order
    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter([stale, fresh]))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda item, *a, **k: by_link[item.link])
    monkeypatch.setattr("rabbit_hole.run.generate", lambda paper, *a, **k: _ep(paper, tmp_path))
    filed = []
    monkeypatch.setattr("rabbit_hole.run.file_episode",
                        lambda e, bucket, p, t: (filed.append(e.paper.title) or (tmp_path / "f.m4a")))
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 2
    assert filed == ["Whale fresh", "Whale stale"]  # fresh scores higher -> filed first


def test_run_pipeline_boosts_audio_before_filing(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config()
    item = _item("Whale song", "http://x/10.1234/whale")
    paper = _paper_for(item, doi="10.1234/whale")
    events = []

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter([item]))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda *a, **k: paper)
    monkeypatch.setattr("rabbit_hole.run.generate", lambda *a, **k: _ep(paper, tmp_path))
    monkeypatch.setattr("rabbit_hole.run.file_episode",
                        lambda e, b, p, t: (events.append(("file", e.audio_path)) or (tmp_path / "f.m4a")))
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")
    monkeypatch.setattr("rabbit_hole.run.boost_loudness",
                        lambda path, **k: events.append(("boost", path)))

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 1
    # boost runs on the generated path, before filing
    assert events == [("boost", tmp_path / "a.m4a"), ("file", tmp_path / "a.m4a")]


def test_run_pipeline_files_even_when_boost_fails(tmp_path, monkeypatch):
    from rabbit_hole.audio import AudioBoostError
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config()
    item = _item("Whale song", "http://x/10.1234/whale")
    paper = _paper_for(item, doi="10.1234/whale")
    filed = []

    def boom(*a, **k):
        raise AudioBoostError("ffmpeg blew up")

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter([item]))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda *a, **k: paper)
    monkeypatch.setattr("rabbit_hole.run.generate", lambda *a, **k: _ep(paper, tmp_path))
    monkeypatch.setattr("rabbit_hole.run.file_episode",
                        lambda e, b, p, t: (filed.append(e) or (tmp_path / "f.m4a")))
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")
    monkeypatch.setattr("rabbit_hole.run.boost_loudness", boom)

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 1            # fail-soft: episode still filed despite boost failure
    assert len(filed) == 1


def test_run_pipeline_updates_run_row_incrementally(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    run_id = st.start_run()
    cfg = _config(episodes_per_run=2)
    items = [_item(f"Whale paper {i}", f"http://x/p{i}") for i in range(2)]
    by_link = {it.link: _paper_for(it) for it in items}
    counts_seen = []

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter(items))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda item, *a, **k: by_link[item.link])
    monkeypatch.setattr("rabbit_hole.run.generate", lambda paper, *a, **k: _ep(paper, tmp_path))
    monkeypatch.setattr(
        "rabbit_hole.run.file_episode",
        lambda e, b, p, t: (
            counts_seen.append(
                st.conn.execute("SELECT episodes_generated FROM runs WHERE id=?",
                                (run_id,)).fetchone()[0]
            ) or (tmp_path / "f.m4a")
        ),
    )
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path,
                         logger=_NullLog(), run_id=run_id)
    assert count == 2
    # while filing episode 1 the row was still 0/None; while filing episode 2 it was 1
    assert counts_seen[1] == 1
    assert st.conn.execute("SELECT episodes_generated FROM runs WHERE id=?",
                           (run_id,)).fetchone()[0] == 2
    # episodes are linked to the run
    assert st.episode_titles_for_run(run_id) == [
        "Animals - Whale paper 0", "Animals - Whale paper 1"]


def test_startup_recovery_closes_stale_runs_and_notifies(tmp_path, monkeypatch):
    from rabbit_hole.run import _startup_recovery
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    st.start_run()                      # stale: never finished
    posts = []
    monkeypatch.setattr("rabbit_hole.run.post_discord",
                        lambda url, msg: posts.append((url, msg)))
    _startup_recovery(st, "http://hook", _NullLog())
    assert st.close_stale_runs() == 0   # already closed by the helper
    assert posts and "stale" in posts[0][1]


def test_startup_recovery_quiet_when_nothing_stale(tmp_path, monkeypatch):
    from rabbit_hole.run import _startup_recovery
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    posts = []
    monkeypatch.setattr("rabbit_hole.run.post_discord",
                        lambda url, msg: posts.append(msg))
    _startup_recovery(st, "http://hook", _NullLog())
    assert posts == []


def test_pending_row_covers_the_generate_window(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config()
    item = _item("Whale song", "http://x/10.1234/whale")
    paper = _paper_for(item, doi="10.1234/whale")
    during_generate = []

    def fake_generate(p, *a, **k):
        during_generate.extend(st.list_pending())   # row exists while we generate
        return _ep(p, tmp_path)

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter([item]))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda *a, **k: paper)
    monkeypatch.setattr("rabbit_hole.run.generate", fake_generate)
    monkeypatch.setattr("rabbit_hole.run.file_episode", lambda e, b, p, t: tmp_path / "f.m4a")
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert len(during_generate) == 1
    assert during_generate[0]["title"] == "Whale song"
    assert during_generate[0]["topic_bucket"] == "Animals"
    assert st.list_pending() == []                  # cleared after publish


def test_pending_row_cleared_when_generate_fails(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = _config()
    item = _item("Whale song", "http://x/10.1234/whale")
    paper = _paper_for(item, doi="10.1234/whale")

    monkeypatch.setattr("rabbit_hole.run.poll_all", lambda c, s, **k: iter([item]))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda *a, **k: paper)
    monkeypatch.setattr("rabbit_hole.run.generate",
                        lambda *a, **k: (_ for _ in ()).throw(NotebookLMError("boom")))
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 0
    assert st.list_pending() == []   # a clean failure is not an orphan


def test_run_summary_lists_titles_and_failing_sources(tmp_path):
    from rabbit_hole.run import _run_summary
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    run_id = st.start_run()
    st.record_episode(doi=None, title_hash="h1", title="Whale song", language="en",
                      source_name="Nature", topic_bucket="Animals", score=1.0,
                      pdf_path="p.pdf", audio_path="a.m4a", run_id=run_id)
    for _ in range(3):
        st.bump_source_failure("DeadFeed", "boom")

    msg = _run_summary(st, run_id, status="partial", count=1, target=3)
    assert "partial: 1/3" in msg
    assert "Animals - Whale song" in msg
    assert "DeadFeed" in msg


def test_run_summary_handles_empty_run(tmp_path):
    from rabbit_hole.run import _run_summary
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    run_id = st.start_run()
    msg = _run_summary(st, run_id, status="partial", count=0, target=1)
    assert "(none)" in msg


def test_run_pipeline_round_robins_topics(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    cfg = Config.model_validate({
        "schedule": {"cron": "0 7 * * MON", "episodes_per_run": 2},
        "ranking": {"recency_half_life_days": 30},
        "topics": [{"name": "Animals", "weight": 1.0, "keywords": ["whale"]},
                   {"name": "AI studies", "weight": 1.0, "keywords": ["neural"]}],
        "sources": [{"name": "Nature", "type": "rss", "url": "http://x", "weight": 1.0}],
        "notebooklm": {"nlm_command": "nlm"},
        "plex": {"base_url": "http://127.0.0.1:32400/", "library_name": "L",
                 "target_folder": "ignored"},
        "apis": {"unpaywall_email": "e@x.com"},
        "notifications": {"discord_webhook_keyring_key": "k"},
    })
    # Two whale items (fresher = higher score) + one neural item scoring lowest.
    whale1 = _item("Whale one", "http://x/w1", days_old=1)
    whale2 = _item("Whale two", "http://x/w2", days_old=2)
    neural = _item("Neural nets", "http://x/n1", summary="neural", days_old=30)
    by_link = {it.link: _paper_for(it) for it in (whale1, whale2, neural)}
    filed = []

    monkeypatch.setattr("rabbit_hole.run.poll_all",
                        lambda c, s, **k: iter([whale1, whale2, neural]))
    monkeypatch.setattr("rabbit_hole.run.resolve", lambda item, *a, **k: by_link[item.link])
    monkeypatch.setattr("rabbit_hole.run.generate", lambda paper, *a, **k: _ep(paper, tmp_path))
    monkeypatch.setattr("rabbit_hole.run.file_episode",
                        lambda e, b, p, t: (filed.append(e.paper.title) or (tmp_path / "f.m4a")))
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path, logger=_NullLog())
    assert count == 2
    # without interleave both slots would be whales; round-robin gives one each
    assert filed == ["Whale one", "Neural nets"]


def test_run_pipeline_adopts_orphans_before_polling(tmp_path, monkeypatch):
    from rabbit_hole.recovery import RecoveredEpisode
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    run_id = st.start_run()
    cfg = _config(episodes_per_run=1)
    item = _item("Recovered whale", "http://x/r1")
    paper = _paper_for(item)
    pid = st.add_pending(
        run_id=1, doi=None, title=paper.title, summary=paper.abstract, link=item.link,
        source_name="Nature", source_weight=1.0,
        published_at=item.published_at.isoformat(), pdf_path="data/pdfs/r1.pdf",
        language="en", topic_bucket="Animals", score=2.0,
    )
    rec = RecoveredEpisode(_ep(paper, tmp_path), "Animals", 2.0, pid)
    filed = []

    monkeypatch.setattr("rabbit_hole.run.recover_orphans",
                        lambda cmd, s, a, log: [rec])
    monkeypatch.setattr("rabbit_hole.run.poll_all",
                        lambda c, s, **k: (_ for _ in ()).throw(AssertionError("target met by adoption — no poll")))
    monkeypatch.setattr("rabbit_hole.run.file_episode",
                        lambda e, b, p, t: (filed.append(e.paper.title) or (tmp_path / "f.m4a")))
    monkeypatch.setattr("rabbit_hole.run._plex_token", lambda cfg: "token")

    count = run_pipeline(cfg, st, pdf_dir=tmp_path, audio_dir=tmp_path,
                         logger=_NullLog(), run_id=run_id)
    assert count == 1
    assert filed == ["Recovered whale"]
    assert st.list_pending() == []
    assert st.episode_titles_for_run(run_id) == ["Animals - Recovered whale"]
