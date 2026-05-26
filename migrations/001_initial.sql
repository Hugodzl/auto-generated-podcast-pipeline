CREATE TABLE IF NOT EXISTS seen_items (
  source_name TEXT,
  link_hash TEXT,
  first_seen_at TEXT,
  PRIMARY KEY (source_name, link_hash)
);

CREATE TABLE IF NOT EXISTS feed_etag (
  source_name TEXT PRIMARY KEY,
  etag TEXT,
  last_modified TEXT,
  last_polled_at TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY,
  doi TEXT,
  title_hash TEXT,
  title TEXT,
  language TEXT,
  source_name TEXT,
  topic_bucket TEXT,
  score REAL,
  pdf_path TEXT,
  audio_path TEXT,
  generated_at TEXT,
  filed_at TEXT
);

CREATE TABLE IF NOT EXISTS api_cache (
  api TEXT,
  query_hash TEXT,
  response_json TEXT,
  fetched_at TEXT,
  PRIMARY KEY (api, query_hash)
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  episodes_generated INTEGER,
  error TEXT
);
