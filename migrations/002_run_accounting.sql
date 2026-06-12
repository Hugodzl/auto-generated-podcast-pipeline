ALTER TABLE episodes ADD COLUMN run_id INTEGER;

CREATE TABLE IF NOT EXISTS pending_generations (
  id INTEGER PRIMARY KEY,
  run_id INTEGER,
  doi TEXT,
  title TEXT,
  summary TEXT,
  link TEXT,
  source_name TEXT,
  source_weight REAL,
  published_at TEXT,
  pdf_path TEXT,
  language TEXT,
  topic_bucket TEXT,
  score REAL,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
  source_name TEXT PRIMARY KEY,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_failure_at TEXT,
  last_error TEXT
);
