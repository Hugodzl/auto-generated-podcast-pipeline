import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


def hash_str(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


class State:
    def __init__(self, db_path: Path, migrations_dir: Path):
        self.db_path = Path(db_path)
        self.migrations_dir = Path(migrations_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")

    def init(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TEXT)"
        )
        self.conn.commit()
        for sql_file in sorted(self.migrations_dir.glob("*.sql")):
            done = self.conn.execute(
                "SELECT 1 FROM schema_migrations WHERE filename=?", (sql_file.name,)
            ).fetchone()
            if done:
                continue
            script = sql_file.read_text(encoding="utf-8").strip()
            if not script.endswith(";"):
                script += ";"
            # One explicit transaction per migration: the DDL and its tracking row
            # commit together, so a kill mid-migration leaves the DB exactly as
            # before. Without this, executescript auto-commits each statement and
            # non-idempotent DDL (ALTER TABLE in 002) could re-run on restart and
            # fail.
            escaped_name = sql_file.name.replace("'", "''")  # SQL string-literal escaping
            try:
                self.conn.executescript(
                    "BEGIN;\n"
                    f"{script}\n"
                    "INSERT INTO schema_migrations (filename, applied_at) "
                    f"VALUES ('{escaped_name}', '{datetime.utcnow().isoformat()}');\n"
                    "COMMIT;"
                )
            except Exception:
                self.conn.rollback()
                raise

    # --- seen items ---
    def has_seen(self, source_name: str, link_hash: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM seen_items WHERE source_name=? AND link_hash=?",
            (source_name, link_hash),
        )
        return cur.fetchone() is not None

    def mark_seen(self, source_name: str, link_hash: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_items (source_name, link_hash, first_seen_at) VALUES (?,?,?)",
            (source_name, link_hash, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    # --- etag ---
    def get_etag(self, source_name: str) -> tuple[str | None, str | None]:
        cur = self.conn.execute(
            "SELECT etag, last_modified FROM feed_etag WHERE source_name=?",
            (source_name,),
        )
        row = cur.fetchone()
        return (row[0], row[1]) if row else (None, None)

    def set_etag(self, source_name: str, etag: str | None, last_modified: str | None) -> None:
        self.conn.execute(
            "INSERT INTO feed_etag (source_name, etag, last_modified, last_polled_at) "
            "VALUES (?,?,?,?) ON CONFLICT(source_name) DO UPDATE SET "
            "etag=excluded.etag, last_modified=excluded.last_modified, last_polled_at=excluded.last_polled_at",
            (source_name, etag, last_modified, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    # --- episodes ---
    def has_episode(self, doi: str | None, title_hash: str) -> bool:
        if doi:
            cur = self.conn.execute(
                "SELECT 1 FROM episodes WHERE doi=? OR title_hash=?", (doi, title_hash)
            )
        else:
            cur = self.conn.execute(
                "SELECT 1 FROM episodes WHERE title_hash=?", (title_hash,)
            )
        return cur.fetchone() is not None

    def record_episode(self, *, doi, title_hash, title, language, source_name,
                       topic_bucket, score, pdf_path, audio_path, run_id=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO episodes (doi, title_hash, title, language, source_name, "
            "topic_bucket, score, pdf_path, audio_path, generated_at, filed_at, run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (doi, title_hash, title, language, source_name, topic_bucket, score,
             pdf_path, audio_path, datetime.utcnow().isoformat(), None, run_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def episode_titles_for_run(self, run_id: int) -> list[str]:
        cur = self.conn.execute(
            "SELECT topic_bucket, title FROM episodes WHERE run_id=? ORDER BY id",
            (run_id,),
        )
        return [f"{bucket} - {title}" for bucket, title in cur.fetchall()]

    # --- pending generations (crash recovery) ---
    _PENDING_COLS = ("id", "run_id", "doi", "title", "summary", "link",
                     "source_name", "source_weight", "published_at", "pdf_path",
                     "language", "topic_bucket", "score", "created_at")

    def add_pending(self, *, run_id, doi, title, summary, link, source_name,
                    source_weight, published_at, pdf_path, language,
                    topic_bucket, score) -> int:
        cur = self.conn.execute(
            "INSERT INTO pending_generations (run_id, doi, title, summary, link, "
            "source_name, source_weight, published_at, pdf_path, language, "
            "topic_bucket, score, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, doi, title, summary, link, source_name, source_weight,
             published_at, pdf_path, language, topic_bucket, score,
             datetime.utcnow().isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_pending(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT " + ", ".join(self._PENDING_COLS) + " FROM pending_generations ORDER BY id"
        )
        return [dict(zip(self._PENDING_COLS, row)) for row in cur.fetchall()]

    def delete_pending(self, pending_id: int) -> None:
        self.conn.execute("DELETE FROM pending_generations WHERE id=?", (pending_id,))
        self.conn.commit()

    # --- source health ---
    def bump_source_failure(self, source_name: str, error: str) -> int:
        self.conn.execute(
            "INSERT INTO source_health (source_name, consecutive_failures, "
            "last_failure_at, last_error) VALUES (?,1,?,?) "
            "ON CONFLICT(source_name) DO UPDATE SET "
            "consecutive_failures=consecutive_failures+1, "
            "last_failure_at=excluded.last_failure_at, last_error=excluded.last_error",
            (source_name, datetime.utcnow().isoformat(), error),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT consecutive_failures FROM source_health WHERE source_name=?",
            (source_name,),
        ).fetchone()[0]

    def reset_source_failures(self, source_name: str) -> None:
        self.conn.execute(
            "UPDATE source_health SET consecutive_failures=0 WHERE source_name=?",
            (source_name,),
        )
        self.conn.commit()

    def failing_sources(self, threshold: int = 3) -> list[tuple[str, int]]:
        cur = self.conn.execute(
            "SELECT source_name, consecutive_failures FROM source_health "
            "WHERE consecutive_failures>=? ORDER BY source_name", (threshold,),
        )
        return [(name, n) for name, n in cur.fetchall()]

    # --- api cache ---
    def cache_get(self, api: str, query_hash: str, ttl_days: float) -> dict | None:
        cur = self.conn.execute(
            "SELECT response_json, fetched_at FROM api_cache WHERE api=? AND query_hash=?",
            (api, query_hash),
        )
        row = cur.fetchone()
        if not row:
            return None
        fetched = datetime.fromisoformat(row[1])
        if datetime.utcnow() - fetched > timedelta(days=ttl_days):
            return None
        return json.loads(row[0])

    def cache_set(self, api: str, query_hash: str, response: dict) -> None:
        self.conn.execute(
            "INSERT INTO api_cache (api, query_hash, response_json, fetched_at) "
            "VALUES (?,?,?,?) ON CONFLICT(api, query_hash) DO UPDATE SET "
            "response_json=excluded.response_json, fetched_at=excluded.fetched_at",
            (api, query_hash, json.dumps(response), datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    # --- runs ---
    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
            (datetime.utcnow().isoformat(),),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_run_episodes(self, run_id: int, episodes_generated: int) -> None:
        self.conn.execute(
            "UPDATE runs SET episodes_generated=? WHERE id=?",
            (episodes_generated, run_id),
        )
        self.conn.commit()

    def finish_run(self, run_id: int, status: str, error: str | None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, error=? WHERE id=?",
            (datetime.utcnow().isoformat(), status, error, run_id),
        )
        self.conn.commit()

    def close_stale_runs(self) -> int:
        """Close out 'running' rows left behind by a killed process. Call BEFORE
        start_run, or the fresh row gets aborted too."""
        cur = self.conn.execute(
            "UPDATE runs SET finished_at=?, status='aborted', "
            "error='stale: process died without finishing' WHERE status='running'",
            (datetime.utcnow().isoformat(),),
        )
        self.conn.commit()
        return cur.rowcount
