"""One-off: rescue the orphaned episode 3 of run 5 (2026-06-11).

The pipeline process was killed mid-run (host session closed) after the
NotebookLM generation for arXiv 2606.11605 had been requested. The audio
completed server-side overnight; the artifact was downloaded manually to
data/audio/. This script replays the pipeline's post-generation steps —
boost -> file -> record — with the paper metadata rebuilt from the arXiv
API entry and the original feed link recovered from seen_items.

Run from the project root: uv run python scripts/rescue_run5_episode3.py
"""
from datetime import datetime, timezone
from pathlib import Path

import keyring

from rabbit_hole.audio import boost_loudness
from rabbit_hole.config import load_config
from rabbit_hole.notebooklm import GeneratedEpisode
from rabbit_hole.plex import file_episode
from rabbit_hole.ranker import score_item
from rabbit_hole.resolver import Paper, detect_language
from rabbit_hole.sources import FeedItem
from rabbit_hole.state import State, hash_str

TITLE = ("Physics-Distilled Neural Network enabled by Large Language Models "
         "for Manufacturing Process-Property Predictive Modeling")
SUMMARY = (
    "Predicting process-property relationships in manufacturing is often "
    "challenged by high experimental costs and the limited interpretability "
    "of complex 'black-box' models. This paper proposes a novel knowledge "
    "distillation framework designed to achieve high-accuracy predictions in "
    "data-scarce scenarios. The framework integrates analytical physics "
    "priors, which are systematically extracted from scientific literature "
    "via Large Language Models, into a privileged teacher model."
)
LINK = "https://arxiv.org/abs/2606.11605"  # hash_str(LINK) == PDF stem below
SOURCE_NAME = "arXiv cs.AI"                # from seen_items for that hash
PUBLISHED = datetime(2026, 6, 10, 3, 5, 42, tzinfo=timezone.utc)
PDF = Path("data/pdfs/c439dad8478c3c7f49357426e6068d6c57b82d39.pdf")
AUDIO = Path("data/audio/bf5cfb76-f8e7-4846-8479-7a54f6e94a79_audio.m4a")
NOTEBOOK_ID = "bf5cfb76-f8e7-4846-8479-7a54f6e94a79"


def main() -> None:
    assert PDF.exists(), PDF
    assert AUDIO.exists(), AUDIO
    cfg = load_config(Path("config/config.toml"))
    state = State(Path("data/state.db"), Path("migrations"))

    weight = next(s.weight for s in cfg.sources if s.name == SOURCE_NAME)
    item = FeedItem(source_name=SOURCE_NAME, source_weight=weight, title=TITLE,
                    summary=SUMMARY, link=LINK, published_at=PUBLISHED)
    scored = score_item(item, cfg.topics, cfg.ranking)
    assert scored.score > 0, "rescued item no longer matches any topic"

    paper = Paper(doi=None, title=TITLE, authors=[], abstract=SUMMARY,
                  published_at=PUBLISHED, pdf_path=PDF,
                  language=detect_language(f"{TITLE} {SUMMARY}"), source=item)
    ep = GeneratedEpisode(paper=paper, audio_path=AUDIO, duration_seconds=0,
                          nlm_notebook_id=NOTEBOOK_ID)

    boost_loudness(ep.audio_path, gain=cfg.audio.gain_factor)
    token = keyring.get_password("rabbit_hole", "plex_token") or ""
    dest = file_episode(ep, scored.topic_bucket, cfg.plex, token)
    state.record_episode(
        doi=None, title_hash=hash_str(TITLE), title=TITLE,
        language=paper.language, source_name=SOURCE_NAME,
        topic_bucket=scored.topic_bucket, score=scored.score,
        pdf_path=str(PDF), audio_path=str(dest),
    )
    print(f"filed: {dest}")
    print(f"bucket: {scored.topic_bucket} | score: {scored.score:.3f} | lang: {paper.language}")


if __name__ == "__main__":
    main()
