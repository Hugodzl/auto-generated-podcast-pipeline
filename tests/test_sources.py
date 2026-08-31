from datetime import datetime

import respx
import httpx

from rabbit_hole.config import SourceConfig
from rabbit_hole.sources import FeedItem, parse_feed, poll_source, poll_all
from rabbit_hole.state import State

SAMPLE_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Sperm whales have vowels</title>
    <description>A study of cetacean communication.</description>
    <link>https://www.nature.com/articles/s41586-024-12345</link>
    <pubDate>Mon, 19 May 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_parse_feed_extracts_items():
    src = SourceConfig(name="Nature", type="rss", url="http://x", weight=1.0)
    items = parse_feed(src, SAMPLE_RSS)
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, FeedItem)
    assert item.title == "Sperm whales have vowels"
    assert item.source_name == "Nature"
    assert item.source_weight == 1.0
    assert item.link.endswith("12345")
    assert isinstance(item.published_at, datetime)


@respx.mock
def test_poll_source_dedups_seen(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    src = SourceConfig(name="Nature", type="rss", url="http://x/feed", weight=1.0)
    respx.get("http://x/feed").mock(return_value=httpx.Response(200, text=SAMPLE_RSS))

    first = list(poll_source(src, st))
    assert len(first) == 1
    # second poll: item already marked seen -> nothing new
    respx.get("http://x/feed").mock(return_value=httpx.Response(200, text=SAMPLE_RSS))
    second = list(poll_source(src, st))
    assert len(second) == 0


@respx.mock
def test_poll_source_disabled_skipped(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    src = SourceConfig(name="Nature", type="rss", url="http://x/feed", weight=1.0, enabled=False)
    assert list(poll_source(src, st)) == []


class _SpyLog:
    def __init__(self):
        self.events = []
    def info(self, event, **kw):
        self.events.append((event, kw))
    def warning(self, event, **kw):
        self.events.append((event, kw))
    def error(self, event, **kw):
        self.events.append((event, kw))


def test_poll_all_logs_failures_and_counts(tmp_path, monkeypatch):
    from pathlib import Path
    from rabbit_hole.config import Config
    from rabbit_hole.state import State
    migrations = Path(__file__).parent.parent / "migrations"
    st = State(tmp_path / "state.db", migrations)
    st.init()
    cfg = Config.model_validate({
        "schedule": {"cron": "0 7 * * MON", "episodes_per_run": 1},
        "ranking": {"recency_half_life_days": 30},
        "topics": [{"name": "T", "weight": 1.0, "keywords": ["k"]}],
        "sources": [
            {"name": "Dead", "type": "rss", "url": "http://dead", "weight": 1.0},
            {"name": "Alive", "type": "rss", "url": "http://alive", "weight": 1.0},
        ],
        "notebooklm": {"nlm_command": "nlm"},
        "plex": {"base_url": "http://127.0.0.1:32400/", "library_name": "L",
                 "target_folder": "ignored"},
        "apis": {"unpaywall_email": "e@x.com"},
        "notifications": {"discord_webhook_keyring_key": "k"},
    })

    def fake_poll_source(source, state):
        if source.name == "Dead":
            raise httpx.ConnectError("nope")
        yield from []   # alive but quiet

    monkeypatch.setattr("rabbit_hole.sources.poll_source", fake_poll_source)
    log = _SpyLog()
    list(poll_all(cfg, st, logger=log))

    events = dict(log.events)
    assert "source_poll_failed" in events and events["source_poll_failed"]["source"] == "Dead"
    assert ("source_polled", {"source": "Alive", "new_items": 0}) in log.events
    assert st.bump_source_failure("Dead", "x") == 2   # poll already counted 1
    assert st.failing_sources(threshold=1) == [("Dead", 2)]


def test_poll_all_leaves_disabled_source_health_untouched(tmp_path, monkeypatch):
    from pathlib import Path
    from rabbit_hole.config import Config
    from rabbit_hole.state import State
    migrations = Path(__file__).parent.parent / "migrations"
    st = State(tmp_path / "state.db", migrations)
    st.init()
    st.bump_source_failure("Off", "old failure")   # failing, then operator disables it
    cfg = Config.model_validate({
        "schedule": {"cron": "0 7 * * MON", "episodes_per_run": 1},
        "ranking": {"recency_half_life_days": 30},
        "topics": [{"name": "T", "weight": 1.0, "keywords": ["k"]}],
        "sources": [{"name": "Off", "type": "rss", "url": "http://off",
                     "weight": 1.0, "enabled": False}],
        "notebooklm": {"nlm_command": "nlm"},
        "plex": {"base_url": "http://127.0.0.1:32400/", "library_name": "L",
                 "target_folder": "ignored"},
        "apis": {"unpaywall_email": "e@x.com"},
        "notifications": {"discord_webhook_keyring_key": "k"},
    })

    def _boom(source, state):
        raise AssertionError("disabled source must not be polled")

    monkeypatch.setattr("rabbit_hole.sources.poll_source", _boom)
    log = _SpyLog()
    assert list(poll_all(cfg, st, logger=log)) == []
    assert log.events == []                               # no source_polled for a skip
    assert st.failing_sources(threshold=1) == [("Off", 1)]  # counter NOT reset


def test_parse_feed_marks_items_from_a_url_sourced_feed():
    """Magazine/encyclopedia feeds have no PDF to resolve: their items are handed
    to NotebookLM as URLs, and the flag has to survive the feed -> item hop."""
    src = SourceConfig(name="Aeon", type="rss", url="http://x", weight=1.0, url_source=True)
    assert parse_feed(src, SAMPLE_RSS)[0].url_source is True


def test_parse_feed_items_resolve_to_a_pdf_by_default():
    src = SourceConfig(name="Nature", type="rss", url="http://x", weight=1.0)
    assert parse_feed(src, SAMPLE_RSS)[0].url_source is False
