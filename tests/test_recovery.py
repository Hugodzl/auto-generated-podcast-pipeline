from pathlib import Path

from rabbit_hole import recovery
from rabbit_hole.state import State

MIGRATIONS = Path(__file__).parent.parent / "migrations"


class _NullLog:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _pending(st, pdf_stem="c439", title="T"):
    return st.add_pending(
        run_id=1, doi=None, title=title, summary="whale", link="http://x/1",
        source_name="Nature", source_weight=1.0,
        published_at="2026-06-10T03:05:42+00:00",
        pdf_path=f"data/pdfs/{pdf_stem}.pdf", language="en",
        topic_bucket="Animals", score=1.5,
    )


def test_adopts_completed_orphan(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    pid = _pending(st)
    deleted = []
    monkeypatch.setattr(recovery, "_nlm_json", lambda cmd, args, t:
        [{"id": "nb1", "title": "rabbit-hole c439 deadbeef"}]
        if args[:2] == ["list", "notebooks"]
        else [{"type": "audio", "status": "completed", "id": "a1"}])
    monkeypatch.setattr(recovery, "_nlm",
                        lambda cmd, args, t: deleted.append(args))
    monkeypatch.setattr(recovery, "_download_audio",
                        lambda cmd, nb, art, out: out.write_bytes(b"x") or True)

    got = recovery.recover_orphans("nlm", st, tmp_path, _NullLog())
    assert len(got) == 1
    r = got[0]
    assert r.pending_id == pid
    assert r.topic_bucket == "Animals" and r.score == 1.5
    assert r.episode.paper.title == "T"
    assert r.episode.audio_path.exists()
    assert ["notebook", "delete", "nb1", "--confirm"] in deleted
    assert st.list_pending() != []   # publish/cleanup is the orchestrator's job


def test_deletes_unmatched_orphan_and_purges_stale_pending(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    _pending(st, pdf_stem="zzz")     # no notebook matches this stem
    deleted = []
    monkeypatch.setattr(recovery, "_nlm_json", lambda cmd, args, t:
        [{"id": "nb9", "title": "rabbit-hole unknownstem 12ab34cd"},
         {"id": "nb2", "title": "My personal notebook"}]
        if args[:2] == ["list", "notebooks"] else [])
    monkeypatch.setattr(recovery, "_nlm",
                        lambda cmd, args, t: deleted.append(args))

    got = recovery.recover_orphans("nlm", st, tmp_path, _NullLog())
    assert got == []
    assert ["notebook", "delete", "nb9", "--confirm"] in deleted   # orphan removed
    assert not any("nb2" in a for a in deleted)                    # non-prefixed untouched
    assert st.list_pending() == []                                 # stale row purged


def test_incomplete_artifact_is_dropped(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    _pending(st)
    deleted = []
    monkeypatch.setattr(recovery, "_nlm_json", lambda cmd, args, t:
        [{"id": "nb1", "title": "rabbit-hole c439 deadbeef"}]
        if args[:2] == ["list", "notebooks"]
        else [{"type": "audio", "status": "in_progress", "id": "a1"}])
    monkeypatch.setattr(recovery, "_nlm",
                        lambda cmd, args, t: deleted.append(args))

    got = recovery.recover_orphans("nlm", st, tmp_path, _NullLog())
    assert got == []
    assert ["notebook", "delete", "nb1", "--confirm"] in deleted
    assert st.list_pending() == []   # candidate is gone; logged as unrecoverable


def test_download_failure_keeps_notebook_and_snapshot(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    _pending(st)
    deleted = []
    monkeypatch.setattr(recovery, "_nlm_json", lambda cmd, args, t:
        [{"id": "nb1", "title": "rabbit-hole c439 deadbeef"}]
        if args[:2] == ["list", "notebooks"]
        else [{"type": "audio", "status": "completed", "id": "a1"}])
    monkeypatch.setattr(recovery, "_nlm",
                        lambda cmd, args, t: deleted.append(args))
    monkeypatch.setattr(recovery, "_download_audio",
                        lambda cmd, nb, art, out: False)   # download never succeeds

    got = recovery.recover_orphans("nlm", st, tmp_path, _NullLog())
    assert got == []
    assert deleted == []                 # notebook kept for retry next run
    assert len(st.list_pending()) == 1   # snapshot kept too


def test_unreachable_nlm_changes_nothing(tmp_path, monkeypatch):
    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    _pending(st)
    monkeypatch.setattr(recovery, "_nlm_json", lambda cmd, args, t: None)  # nlm down

    got = recovery.recover_orphans("nlm", st, tmp_path, _NullLog())
    assert got == []
    assert len(st.list_pending()) == 1   # nothing purged while nlm is unreachable


def test_adopts_an_orphan_generated_from_a_url_source(tmp_path, monkeypatch):
    """A url-sourced pending row has no pdf_path to key on, so recovery matches
    the notebook label the run used: the hash of the item link."""
    from rabbit_hole.state import hash_str

    st = State(tmp_path / "state.db", MIGRATIONS)
    st.init()
    link = "https://aeon.co/essays/whales"
    pid = st.add_pending(
        run_id=1, doi=None, title="Whale essay", summary="essay", link=link,
        source_name="Aeon", source_weight=0.8,
        published_at="2026-08-30T03:05:42+00:00", pdf_path=None, language="en",
        topic_bucket="Technology and society", score=1.5,
    )
    monkeypatch.setattr(recovery, "_nlm_json", lambda cmd, args, t:
        [{"id": "nb1", "title": f"rabbit-hole {hash_str(link)} deadbeef"}]
        if args[:2] == ["list", "notebooks"]
        else [{"type": "audio", "status": "completed", "id": "a1"}])
    monkeypatch.setattr(recovery, "_nlm", lambda cmd, args, t: None)
    monkeypatch.setattr(recovery, "_download_audio",
                        lambda cmd, nb, art, out: out.write_bytes(b"x") or True)

    got = recovery.recover_orphans("nlm", st, tmp_path, _NullLog())
    assert len(got) == 1
    assert got[0].pending_id == pid
    assert got[0].episode.paper.pdf_path is None
