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
        for sql_file in sorted(self.migrations_dir.glob("*.sql")):
            self.conn.executescript(sql_file.read_text(encoding="utf-8"))
        self.conn.commit()

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
                       topic_bucket, score, pdf_path, audio_path) -> int:
        cur = self.conn.execute(
            "INSERT INTO episodes (doi, title_hash, title, language, source_name, "
            "topic_bucket, score, pdf_path, audio_path, generated_at, filed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doi, title_hash, title, language, source_name, topic_bucket, score,
             pdf_path, audio_path, datetime.utcnow().isoformat(), None),
        )
        self.conn.commit()
        return cur.lastrowid

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

    def finish_run(self, run_id: int, status: str, episodes_generated: int, error: str | None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, episodes_generated=?, error=? WHERE id=?",
            (datetime.utcnow().isoformat(), status, episodes_generated, error, run_id),
        )
        self.conn.commit()
