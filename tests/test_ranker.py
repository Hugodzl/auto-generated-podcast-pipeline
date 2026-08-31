from datetime import datetime, timedelta, timezone

from rabbit_hole.config import RankingConfig, TopicConfig
from rabbit_hole.ranker import ScoredItem, interleave_topics, keyword_hits, rank, recency_decay, score_item
from rabbit_hole.sources import FeedItem

TOPICS = [
    TopicConfig(name="AI studies", weight=1.0, keywords=["neural network", "transformer"]),
    TopicConfig(name="Animals", weight=1.0, keywords=["whale", "primate"]),
]


def make_item(title, summary, days_old=0, source_weight=1.0, link="http://x"):
    pub = datetime.now(timezone.utc) - timedelta(days=days_old)
    return FeedItem(source_name="S", source_weight=source_weight, title=title,
                    summary=summary, link=link, published_at=pub, raw={})


def test_keyword_hits_counts_unique_keywords():
    assert keyword_hits("A transformer neural network", ["neural network", "transformer"]) == 2
    assert keyword_hits("nothing relevant", ["whale"]) == 0


def test_recency_decay_halves_at_half_life():
    cfg = RankingConfig(recency_half_life_days=30)
    decay = recency_decay(datetime.now(timezone.utc) - timedelta(days=30), cfg)
    assert 0.49 < decay < 0.51


def test_score_item_picks_best_topic_bucket():
    cfg = RankingConfig(recency_half_life_days=30)
    it = make_item("Whale song", "primate and whale cognition", days_old=0)
    scored = score_item(it, TOPICS, cfg)
    assert isinstance(scored, ScoredItem)
    assert scored.item is it
    assert scored.topic_bucket == "Animals"
    assert scored.score > 0


def test_rank_orders_by_score_descending():
    cfg = RankingConfig(recency_half_life_days=30)
    fresh = make_item("Transformer study", "neural network transformer", days_old=1, link="http://a")
    stale = make_item("Old transformer", "neural network", days_old=300, link="http://b")
    ranked = rank([stale, fresh], TOPICS, cfg)
    assert [s.item.link for s in ranked] == ["http://a", "http://b"]  # fresh first


def test_rank_returns_full_list_not_truncated():
    cfg = RankingConfig(recency_half_life_days=30)
    items = [make_item(f"Transformer {i}", "neural network", days_old=i, link=f"http://{i}")
             for i in range(20)]
    ranked = rank(items, TOPICS, cfg)
    assert len(ranked) == 20  # rank no longer slices to a limit


def test_rank_drops_zero_score_items():
    cfg = RankingConfig(recency_half_life_days=30)
    irrelevant = make_item("Cooking pasta", "recipes and food", days_old=1)
    ranked = rank([irrelevant], TOPICS, cfg)
    assert ranked == []


def _scored(topic, score):
    return ScoredItem(item=None, score=score, topic_bucket=topic)


def test_interleave_cycles_topics():
    ranked = [_scored("AI", 2.0), _scored("AI", 1.9), _scored("AI", 1.8),
              _scored("Animals", 1.0), _scored("Psych", 0.5)]
    out = interleave_topics(ranked)
    assert [(s.topic_bucket, s.score) for s in out] == [
        ("AI", 2.0), ("Animals", 1.0), ("Psych", 0.5),   # cycle 1: each topic's best
        ("AI", 1.9),                                      # cycle 2
        ("AI", 1.8),                                      # cycle 3
    ]


def test_interleave_single_topic_is_identity():
    ranked = [_scored("AI", 2.0), _scored("AI", 1.0)]
    assert interleave_topics(ranked) == ranked


def test_interleave_empty():
    assert interleave_topics([]) == []


def test_keyword_hits_ignores_a_keyword_buried_inside_a_longer_word():
    # Episode 8 shipped as "Human relationships and psychology" because a paper on
    # potassium dynamics says "coupled" ten times and "couple" is a psych keyword.
    assert keyword_hits("a nonlinear term coupled to astrocyte dynamics", ["couple"]) == 0
    assert keyword_hits("we describe the tool used in the study", ["tool use"]) == 0
    assert keyword_hits("the attachments were removed", ["attachment"]) == 1  # plural still counts


def test_keyword_hits_still_matches_plural_forms():
    assert keyword_hits("sperm whales sing", ["whale"]) == 1
    assert keyword_hits("two transformers were trained", ["transformer"]) == 1
    assert keyword_hits("romantic relationships in adulthood", ["romantic relationship"]) == 1


def test_coupled_dynamics_paper_does_not_land_in_the_relationships_bucket():
    cfg = RankingConfig(recency_half_life_days=30)
    topics = [TopicConfig(name="Human relationships and psychology", weight=1.0,
                          keywords=["couple", "attachment", "intimacy"]),
              TopicConfig(name="AI studies", weight=1.0, keywords=["neural network"])]
    it = make_item("An astro-neural-field model with application to cortical spreading depolarization",
                   "The potential variables are coupled to a third state variable for "
                   "astrocytic K+ uptake, coupled with a diffusive term.")
    assert score_item(it, topics, cfg).topic_bucket != "Human relationships and psychology"


def test_a_paper_matching_many_keywords_beats_a_one_word_match_from_a_heavier_feed():
    """A live dry run put "Ecophysiology: how pollinators use microclimates" at the
    top of AI studies -- one incidental keyword from a weight-0.9 journal feed
    outscoring a three-keyword AI paper from a weight-0.6 preprint feed. Topic
    value has to separate a passing mention from a paper that is actually about
    the topic."""
    cfg = RankingConfig(recency_half_life_days=30)
    topics = [TopicConfig(name="AI studies", weight=1.0,
                          keywords=["neural network", "transformer", "deep learning"])]
    passing_mention = make_item("Pollinator thermal limits", "a neural network was fitted",
                                source_weight=0.9, link="http://bio")
    actually_about_it = make_item("Transformer scaling", "deep learning with a transformer "
                                  "and a neural network", source_weight=0.6, link="http://ai")
    ranked = rank([passing_mention, actually_about_it], topics, cfg)
    assert [s.item.link for s in ranked] == ["http://ai", "http://bio"]
