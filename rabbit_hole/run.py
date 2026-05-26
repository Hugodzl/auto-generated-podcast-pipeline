import sys
from pathlib import Path

import keyring

from rabbit_hole.config import Config, load_config
from rabbit_hole.logging_config import configure_logging, post_discord
from rabbit_hole.notebooklm import NotebookLMError, generate
from rabbit_hole.plex import file_episode
from rabbit_hole.ranker import rank
from rabbit_hole.resolver import resolve
from rabbit_hole.sources import poll_all
from rabbit_hole.state import State, hash_str

CONFIG_PATH = Path("config/config.toml")
DATA_DIR = Path("data")
PDF_DIR = DATA_DIR / "pdfs"
AUDIO_DIR = DATA_DIR / "audio"
LOG_DIR = Path("logs")
MIGRATIONS = Path("migrations")
KEYRING_SERVICE = "rabbit_hole"
MAX_CONSECUTIVE_FAILURES = 5


def _plex_token(cfg: Config) -> str:
    return keyring.get_password(KEYRING_SERVICE, "plex_token") or ""


def run_pipeline(cfg: Config, state: State, pdf_dir: Path, audio_dir: Path, logger) -> int:
    # 1. ingest
    items = list(poll_all(cfg, state))
    logger.info("ingested", count=len(items))

    # 2. resolve to PDFs
    papers = []
    for item in items:
        paper = resolve(item, cfg.apis, state, pdf_dir=pdf_dir)
        if paper:
            papers.append(paper)
    logger.info("resolved", count=len(papers))

    # 3. rank, skipping already-published papers
    papers = [p for p in papers
              if not state.has_episode(doi=p.doi, title_hash=hash_str(p.title))]
    scored = rank(papers, cfg.topics, cfg.ranking, limit=cfg.schedule.episodes_per_run)
    logger.info("ranked", count=len(scored))

    # 4. generate + file
    token = _plex_token(cfg)
    generated = 0
    failures = 0
    for s in scored:
        try:
            ep = generate(s.paper, cfg.notebooklm, out_dir=audio_dir)
        except NotebookLMError as e:
            failures += 1
            logger.error("nlm_failed", title=s.paper.title, error=str(e))
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise
            continue
        dest = file_episode(ep, s.topic_bucket, cfg.plex, token)
        state.record_episode(
            doi=s.paper.doi, title_hash=hash_str(s.paper.title), title=s.paper.title,
            language=s.paper.language, source_name=s.paper.source.source_name,
            topic_bucket=s.topic_bucket, score=s.score,
            pdf_path=str(s.paper.pdf_path), audio_path=str(dest),
        )
        generated += 1
        logger.info("filed", title=s.paper.title, path=str(dest))
    return generated


def main() -> int:
    cfg = load_config(CONFIG_PATH)
    webhook = keyring.get_password(KEYRING_SERVICE, cfg.notifications.discord_webhook_keyring_key)
    logger = configure_logging(LOG_DIR, webhook)
    state = State(DATA_DIR / "state.db", MIGRATIONS)
    state.init()
    run_id = state.start_run()
    try:
        count = run_pipeline(cfg, state, pdf_dir=PDF_DIR, audio_dir=AUDIO_DIR, logger=logger)
        state.finish_run(run_id, status="ok" if count else "partial",
                         episodes_generated=count, error=None)
        return 0
    except Exception as e:  # noqa: BLE001 - top-level guard
        logger.error("run_failed", error=str(e))
        if webhook:
            post_discord(webhook, f"Rabbit Hole run failed: {e}")
        state.finish_run(run_id, status="failed", episodes_generated=0, error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
