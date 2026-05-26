from datetime import datetime

import respx
import httpx

from rabbit_hole.config import SourceConfig
from rabbit_hole.sources import FeedItem, parse_feed, poll_source
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
