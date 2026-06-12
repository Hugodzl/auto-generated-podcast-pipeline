import sqlite3
from datetime import datetime, timedelta

import pytest

from rabbit_hole.state import State


def test_init_creates_tables(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    assert tmp_db.exists()


def test_seen_items_roundtrip(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    assert st.has_seen("Nature", "abc") is False
    st.mark_seen("Nature", "abc")
    assert st.has_seen("Nature", "abc") is True


def test_etag_roundtrip(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    assert st.get_etag("Nature") == (None, None)
    st.set_etag("Nature", etag="W/123", last_modified="Mon")
    assert st.get_etag("Nature") == ("W/123", "Mon")


def test_episode_dedup(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    assert st.has_episode(doi="10.1/x", title_hash="h1") is False
    st.record_episode(
        doi="10.1/x", title_hash="h1", title="T", language="en",
        source_name="Nature", topic_bucket="AI studies", score=1.2,
        pdf_path="p.pdf", audio_path="a.m4a",
    )
    assert st.has_episode(doi="10.1/x", title_hash="h1") is True
    # match by title_hash even when doi missing
    assert st.has_episode(doi=None, title_hash="h1") is True


def test_api_cache_ttl(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    assert st.cache_get("unpaywall", "q1", ttl_days=30) is None
    st.cache_set("unpaywall", "q1", {"ok": True})
    assert st.cache_get("unpaywall", "q1", ttl_days=30) == {"ok": True}
    # expired entry returns None
    old = (datetime.utcnow() - timedelta(days=40)).isoformat()
    st.conn.execute("UPDATE api_cache SET fetched_at=? WHERE api='unpaywall'", (old,))
    assert st.cache_get("unpaywall", "q1", ttl_days=30) is None


def test_run_lifecycle(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    run_id = st.start_run()
    st.finish_run(run_id, status="ok", episodes_generated=5, error=None)
    row = st.conn.execute("SELECT status, episodes_generated FROM runs WHERE id=?", (run_id,)).fetchone()
    assert row[0] == "ok" and row[1] == 5


def test_failed_migration_applies_nothing(tmp_path):
    bad_dir = tmp_path / "migrations"
    bad_dir.mkdir()
    (bad_dir / "001_bad.sql").write_text(
        "CREATE TABLE half_done (x TEXT);\n"
        "ALTER TABLE does_not_exist ADD COLUMN y;\n",
        encoding="utf-8",
    )
    st = State(tmp_path / "state.db", bad_dir)
    with pytest.raises(sqlite3.OperationalError):
        st.init()
    # the failed migration's partial DDL rolled back with the transaction...
    tables = {r[0] for r in st.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "half_done" not in tables
    # ...and it is not recorded as applied
    assert st.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0


def test_migrations_apply_once(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    applied = st.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert applied == len(list(migrations_dir.glob("*.sql")))
    st.init()  # second init: idempotent, re-applies nothing, raises nothing
    assert st.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == applied
