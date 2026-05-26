from datetime import datetime, timedelta, timezone
from pathlib import Path

from rabbit_hole.config import RankingConfig, TopicConfig
from rabbit_hole.ranker import ScoredPaper, keyword_hits, recency_decay, score_paper, rank
from rabbit_hole.resolver import Paper
from rabbit_hole.sources import FeedItem

TOPICS = [
    TopicConfig(name="AI studies", weight=1.0, keywords=["neural network", "transformer"]),
    TopicConfig(name="Animals", weight=1.0, keywords=["whale", "primate"]),
]


def make_paper(title, abstract, days_old=0, source_weight=1.0, doi="10.1/x"):
    pub = datetime.now(timezone.utc) - timedelta(days=days_old)
    item = FeedItem(source_name="S", source_weight=source_weight, title=title,
                    summary=abstract, link="http://x", published_at=pub, raw={})
    return Paper(doi=doi, title=title, authors=[], abstract=abstract,
                 published_at=pub, pdf_path=Path("p.pdf"), language="en", source=item)


def test_keyword_hits_counts_unique_keywords():
    assert keyword_hits("A transformer neural network", ["neural network", "transformer"]) == 2
    assert keyword_hits("nothing relevant", ["whale"]) == 0


def test_recency_decay_halves_at_half_life():
    cfg = RankingConfig(recency_half_life_days=30)
    decay = recency_decay(datetime.now(timezone.utc) - timedelta(days=30), cfg)
    assert 0.49 < decay < 0.51


def test_score_picks_best_topic_bucket():
    cfg = RankingConfig(recency_half_life_days=30)
    p = make_paper("Whale song", "primate and whale cognition", days_old=0)
    scored = score_paper(p, TOPICS, cfg)
    assert scored.topic_bucket == "Animals"
    assert scored.score > 0


def test_rank_orders_by_score_and_limits():
    cfg = RankingConfig(recency_half_life_days=30)
    fresh = make_paper("Transformer study", "neural network transformer", days_old=1, doi="10.1/a")
    stale = make_paper("Old transformer", "neural network", days_old=300, doi="10.1/b")
    ranked = rank([stale, fresh], TOPICS, cfg, limit=1)
    assert len(ranked) == 1
    assert ranked[0].paper.doi == "10.1/a"


def test_rank_drops_zero_score_papers():
    cfg = RankingConfig(recency_half_life_days=30)
    irrelevant = make_paper("Cooking pasta", "recipes and food", days_old=1)
    ranked = rank([irrelevant], TOPICS, cfg, limit=5)
    assert ranked == []
