from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

import feedparser
import httpx

from rabbit_hole.config import Config, SourceConfig
from rabbit_hole.state import State, hash_str

USER_AGENT = "RandomRabbitHole/1.0 (mailto:you@example.com)"


@dataclass
class FeedItem:
    source_name: str
    source_weight: float
    title: str
    summary: str
    link: str
    published_at: datetime
    raw: dict = field(default_factory=dict)
    url_source: bool = False   # feed the link to NotebookLM instead of resolving a PDF


def _parse_date(entry) -> datetime:
    if getattr(entry, "published_parsed", None):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def parse_feed(source: SourceConfig, body: str) -> list[FeedItem]:
    parsed = feedparser.parse(body)
    items = []
    for entry in parsed.entries:
        items.append(
            FeedItem(
                source_name=source.name,
                source_weight=source.weight,
                title=entry.get("title", "").strip(),
                summary=entry.get("summary", "").strip(),
                link=entry.get("link", "").strip(),
                published_at=_parse_date(entry),
                raw=dict(entry),
                url_source=source.url_source,
            )
        )
    return items


def poll_source(source: SourceConfig, state: State) -> Iterator[FeedItem]:
    if not source.enabled:
        return
    etag, last_modified = state.get_etag(source.name)
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    resp = httpx.get(source.url, headers=headers, timeout=15.0, follow_redirects=True)
    if resp.status_code == 304:
        return
    resp.raise_for_status()
    state.set_etag(
        source.name,
        etag=resp.headers.get("ETag"),
        last_modified=resp.headers.get("Last-Modified"),
    )

    for item in parse_feed(source, resp.text):
        if not item.link:
            continue
        link_hash = hash_str(item.link)
        if state.has_seen(source.name, link_hash):
            continue
        state.mark_seen(source.name, link_hash)
        yield item


def poll_all(config: Config, state: State, logger=None) -> Iterator[FeedItem]:
    for source in config.sources:
        if not source.enabled:
            continue  # skipped, not polled: leave its health counters untouched
        new_items = 0
        try:
            for item in poll_source(source, state):
                new_items += 1
                yield item
        except httpx.HTTPError as e:
            failures = state.bump_source_failure(source.name, str(e))
            if logger:
                logger.warning("source_poll_failed", source=source.name,
                               error=str(e), consecutive_failures=failures)
            continue  # one bad feed shouldn't kill the run
        state.reset_source_failures(source.name)
        if logger:
            logger.info("source_polled", source=source.name, new_items=new_items)
