import sys
import time
from pathlib import Path

import keyring

from rabbit_hole.config import Config, load_config
from rabbit_hole.lockfile import PipelineLockError, acquire_lock
from rabbit_hole.logging_config import configure_logging, post_discord
from rabbit_hole.audio import AudioBoostError, boost_loudness
from rabbit_hole.notebooklm import GeneratedEpisode, NotebookLMError, generate
from rabbit_hole.plex import file_episode
from rabbit_hole.ranker import rank
from rabbit_hole.recovery import recover_orphans
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


def _startup_recovery(state: State, webhook: str | None, logger) -> None:
    stale = state.close_stale_runs()
    if stale:
        logger.warning("stale_runs_closed", count=stale)
        if webhook:
            post_discord(webhook,
                         f"Rabbit Hole: closed {stale} stale run(s) from a previous crash.")


def _publish(ep: GeneratedEpisode, topic_bucket: str, score: float, cfg: Config,
             state: State, token: str, logger, run_id: int | None = None) -> None:
    """Post-generation steps shared by the normal walk and orphan recovery:
    boost (fail-soft) -> file to Plex -> record in state.

    A boost failure files the (quiet) episode rather than dropping it, and is
    deliberately NOT counted against the caller's consecutive-nlm-failure
    budget — it's post-generation."""
    try:
        boost_loudness(ep.audio_path, gain=cfg.audio.gain_factor)
    except AudioBoostError as e:
        logger.error("audio_boost_failed", title=ep.paper.title, error=str(e))
    dest = file_episode(ep, topic_bucket, cfg.plex, token)
    paper = ep.paper
    state.record_episode(
        doi=paper.doi, title_hash=hash_str(paper.title), title=paper.title,
        language=paper.language, source_name=paper.source.source_name,
        topic_bucket=topic_bucket, score=score,
        pdf_path=str(paper.pdf_path), audio_path=str(dest), run_id=run_id,
    )
    logger.info("filed", title=paper.title, path=str(dest))


def run_pipeline(cfg: Config, state: State, pdf_dir: Path, audio_dir: Path,
                 logger, run_id: int | None = None) -> int:
    token = _plex_token(cfg)
    target = cfg.schedule.episodes_per_run
    generated = 0

    # 0. adopt orphans from a previously killed run first: their audio is already
    #    paid for. They count toward this run's target.
    for r in recover_orphans(cfg.notebooklm.nlm_command, state, Path(audio_dir), logger):
        _publish(r.episode, r.topic_bucket, r.score, cfg, state, token, logger, run_id=run_id)
        state.delete_pending(r.pending_id)
        generated += 1
        if run_id is not None:
            state.set_run_episodes(run_id, generated)
    if generated >= target:
        return generated

    # 1. ingest
    items = list(poll_all(cfg, state, logger=logger))
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
        pending_id = state.add_pending(
            run_id=run_id, doi=paper.doi, title=paper.title, summary=paper.abstract,
            link=paper.source.link, source_name=paper.source.source_name,
            source_weight=paper.source.source_weight,
            published_at=paper.published_at.isoformat(), pdf_path=str(paper.pdf_path),
            language=paper.language, topic_bucket=s.topic_bucket, score=s.score,
        )
        try:
            ep = generate(paper, cfg.notebooklm, out_dir=audio_dir)
        except NotebookLMError as e:
            # A clean nlm failure already deleted its notebook (finally block in
            # _run_nlm) — the snapshot would only confuse recovery. Drop it.
            state.delete_pending(pending_id)
            failures += 1
            logger.error("nlm_failed", title=paper.title, error=str(e))
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise
            continue
        # A raise out of _publish leaves the pending row behind on purpose: the
        # notebook is already deleted (generate's finally), so the next run's
        # recovery pass purges the row rather than adopting anything.
        _publish(ep, s.topic_bucket, s.score, cfg, state, token, logger, run_id=run_id)
        state.delete_pending(pending_id)
        generated += 1
        if run_id is not None:
            state.set_run_episodes(run_id, generated)
        failures = 0  # reset: budget is CONSECUTIVE nlm failures
    return generated


def main() -> int:
    started = time.monotonic()
    cfg = load_config(CONFIG_PATH)
    webhook = keyring.get_password(KEYRING_SERVICE, cfg.notifications.discord_webhook_keyring_key)
    logger = configure_logging(LOG_DIR, webhook)
    try:
        lock = acquire_lock(DATA_DIR / "run.lock")
    except PipelineLockError:
        logger.warning("run_already_in_progress")
        if webhook:
            post_discord(webhook, "Rabbit Hole: another run is already in progress — skipped.")
        # Exit 0 on purpose: a contention skip is an intentional no-op, and a
        # non-zero code would make Task Scheduler mark the weekly job failed
        # (and potentially retry). The Discord note above surfaces the overlap.
        return 0
    try:
        state = State(DATA_DIR / "state.db", MIGRATIONS)
        state.init()
        _startup_recovery(state, webhook, logger)
        run_id = state.start_run()
        try:
            count = run_pipeline(cfg, state, pdf_dir=PDF_DIR, audio_dir=AUDIO_DIR,
                                 logger=logger, run_id=run_id)
            status = "ok" if count >= cfg.schedule.episodes_per_run else "partial"
            state.finish_run(run_id, status=status, error=None)
            logger.info("run_complete", status=status, episodes=count,
                        elapsed_s=round(time.monotonic() - started))
            return 0
        except Exception as e:  # noqa: BLE001 - top-level guard
            logger.error("run_failed", error=str(e))
            if webhook:
                post_discord(webhook, f"Rabbit Hole run failed: {e}")
            state.finish_run(run_id, status="failed", error=str(e))
            return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
