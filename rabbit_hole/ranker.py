import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

from rabbit_hole.config import RankingConfig, TopicConfig
from rabbit_hole.sources import FeedItem


@dataclass
class ScoredItem:
    item: FeedItem
    score: float
    topic_bucket: str


@lru_cache(maxsize=512)
def _keyword_re(keyword: str) -> re.Pattern:
    """Match a keyword as a whole word, tolerating only a plural ending.

    A plain substring test filed a potassium-dynamics preprint under "Human
    relationships and psychology", because "couple" lives inside "coupled" --
    and it would do the same for "tool use" inside "tool used" or "alignment"
    inside "sequence alignment". Word boundaries fix that, but a bare boundary
    on the tail would also stop "whale" matching "whales", so one plural suffix
    is allowed: that admits "transformers"/"relationships" while still rejecting
    the -d/-ing/-ment inflections that change the meaning. Cached because a cold
    run scores thousands of items against every keyword of every topic.
    """
    return re.compile(rf"\b{re.escape(keyword)}(?:e?s)?\b", re.IGNORECASE)


def keyword_hits(text: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if _keyword_re(kw).search(text))


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
        # log1p, not 1 + log1p: the old curve gave a single incidental keyword 61%
        # of the value of five, so one stray "neural network" in a pollinator paper
        # outscored a genuine AI paper from a lower-weighted feed. Now 1 hit is 0.69
        # against 1.79 for five, and being *about* the topic beats mentioning it.
        val = t.weight * math.log1p(hits) if hits else 0.0
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


def interleave_topics(ranked: list[ScoredItem]) -> list[ScoredItem]:
    """Reorder a score-desc list so consumption cycles topics round-robin.

    Cycle 1 holds every topic's best item, cycle 2 the second-bests, and so on;
    a topic with an exhausted pool drops out of later cycles. Within a cycle,
    items keep score-desc order, so the strongest topic leads each round without
    monopolizing the run. The orchestrator's resolve-failure fallthrough then
    lands on the NEXT TOPIC's best candidate instead of more of the same topic.
    """
    pools: dict[str, list[ScoredItem]] = {}
    for s in ranked:
        pools.setdefault(s.topic_bucket, []).append(s)
    out: list[ScoredItem] = []
    depth = 0
    while True:
        cycle = sorted((pool[depth] for pool in pools.values() if depth < len(pool)),
                       key=lambda s: s.score, reverse=True)
        if not cycle:
            return out
        out.extend(cycle)
        depth += 1
