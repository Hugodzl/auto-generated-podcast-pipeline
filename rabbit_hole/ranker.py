import math
from dataclasses import dataclass
from datetime import datetime, timezone

from rabbit_hole.config import RankingConfig, TopicConfig
from rabbit_hole.sources import FeedItem


@dataclass
class ScoredItem:
    item: FeedItem
    score: float
    topic_bucket: str


def keyword_hits(text: str, keywords: list[str]) -> int:
    low = text.lower()
    return sum(1 for kw in keywords if kw.lower() in low)


def recency_decay(published_at: datetime, cfg: RankingConfig) -> float:
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age_days = max((now - published_at).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / cfg.ranking_half_life())


def _best_topic(item: FeedItem, topics: list[TopicConfig]) -> tuple[str, float]:
    text = f"{item.title} {item.summary}"
    best_name, best_val = "", 0.0
    for t in topics:
        hits = keyword_hits(text, t.keywords)
        val = t.weight * (1 + math.log1p(hits)) if hits else 0.0
        if val > best_val:
            best_name, best_val = t.name, val
    return best_name, best_val


def score_item(item: FeedItem, topics: list[TopicConfig], cfg: RankingConfig) -> ScoredItem:
    bucket, topic_val = _best_topic(item, topics)
    score = item.source_weight * topic_val * recency_decay(item.published_at, cfg)
    return ScoredItem(item=item, score=score, topic_bucket=bucket)


def rank(items: list[FeedItem], topics: list[TopicConfig], cfg: RankingConfig) -> list[ScoredItem]:
    scored = [score_item(it, topics, cfg) for it in items]
    scored = [s for s in scored if s.score > 0]
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored
