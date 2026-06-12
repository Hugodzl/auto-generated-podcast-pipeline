import sys
from pathlib import Path

import keyring

from rabbit_hole.config import Config, load_config
from rabbit_hole.logging_config import configure_logging, post_discord
from rabbit_hole.audio import AudioBoostError, boost_loudness
from rabbit_hole.notebooklm import NotebookLMError, generate
from rabbit_hole.plex import file_episode
from rabbit_hole.ranker import rank
from rabbit_hole.resolver import resolve, extract_doi
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

    # 2. drop already-published items BEFORE resolving (metadata only, no PDF needed).
    #    extract_doi(item) and hash_str(item.title) are exactly what resolve()/record_episode
    #    use, so this is identical to the former post-resolve dedup, just one step earlier.
    items = [it for it in items
             if not state.has_episode(doi=extract_doi(it), title_hash=hash_str(it.title))]

    # 3. rank the raw items (no PDFs yet); rank returns the full score>0 list, sorted desc.
    ranked = rank(items, cfg.topics, cfg.ranking)
    logger.info("ranked", count=len(ranked))

    # 4. walk the ranked list: resolve lazily, generate + file, stop once enough episodes exist.
    token = _plex_token(cfg)
    target = cfg.schedule.episodes_per_run
    generated = 0
    failures = 0
    for s in ranked:
        if generated >= target:
            break
        paper = resolve(s.item, cfg.apis, state, pdf_dir=pdf_dir)
        if paper is None:
            # No PDF found for this item. Neutral: not an nlm failure, so the
            # consecutive-failure budget is untouched. Advance to the next candidate.
            logger.info("resolve_skipped", title=s.item.title)
            continue
        try:
            ep = generate(paper, cfg.notebooklm, out_dir=audio_dir)
        except NotebookLMError as e:
            failures += 1
            logger.error("nlm_failed", title=paper.title, error=str(e))
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise
            continue
        try:
            boost_loudness(ep.audio_path, gain=cfg.audio.gain_factor)
        except AudioBoostError as e:
            # Non-fatal: file the (quiet) episode rather than drop it. Not counted
            # against the consecutive-nlm-failure budget — it's post-generation.
            logger.error("audio_boost_failed", title=paper.title, error=str(e))
        dest = file_episode(ep, s.topic_bucket, cfg.plex, token)
        state.record_episode(
            doi=paper.doi, title_hash=hash_str(paper.title), title=paper.title,
            language=paper.language, source_name=paper.source.source_name,
            topic_bucket=s.topic_bucket, score=s.score,
            pdf_path=str(paper.pdf_path), audio_path=str(dest),
        )
        generated += 1
        logger.info("filed", title=paper.title, path=str(dest))
        failures = 0  # reset: budget is CONSECUTIVE nlm failures
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
        state.set_run_episodes(run_id, count)
        state.finish_run(run_id, status="ok" if count else "partial", error=None)
        return 0
    except Exception as e:  # noqa: BLE001 - top-level guard
        logger.error("run_failed", error=str(e))
        if webhook:
            post_discord(webhook, f"Rabbit Hole run failed: {e}")
        state.finish_run(run_id, status="failed", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
