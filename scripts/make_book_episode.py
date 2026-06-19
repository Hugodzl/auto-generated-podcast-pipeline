"""One-off: turn a single book PDF into a long NotebookLM deep-dive episode and
file it into Plex exactly like a routine pipeline episode.

Built 2026-06-19 for Derek Parfit's *Reasons and Persons* (user-supplied PDF).
It bypasses the RSS -> resolve -> rank front of the pipeline and reuses the back
end unchanged:  generate(long deep_dive + focus) -> boost_loudness(+40%) ->
file_episode(Plex).  No RSS state is touched, so this can't collide with the
weekly run's dedup/recovery.

Two modes:
  python scripts/make_book_episode.py --check   # fast preflight, no NLM time spent
  python scripts/make_book_episode.py           # full generate + boost + file

The real run is meant to be launched DETACHED (scripts/run_book_episode_detached.ps1)
so closing the terminal / Claude session can't kill it mid-generation -- the
process-tree death that cost us episode 3 of Run 5.
"""
import subprocess
import sys
from pathlib import Path

# This file lives in scripts/, but it imports the rabbit_hole package at the repo
# root. Running it as a plain script puts scripts/ (not root) on sys.path, so the
# import would fail under the Scheduled Task; add the root explicitly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shutil  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import keyring  # noqa: E402

from rabbit_hole.audio import AudioBoostError, boost_loudness  # noqa: E402
from rabbit_hole.config import load_config  # noqa: E402
from rabbit_hole.logging_config import configure_logging, post_discord  # noqa: E402
from rabbit_hole.notebooklm import NotebookLMError, generate  # noqa: E402
from rabbit_hole.plex import file_episode  # noqa: E402
from rabbit_hole.resolver import Paper  # noqa: E402
from rabbit_hole.sources import FeedItem  # noqa: E402

CONFIG_PATH = ROOT / "config" / "config.toml"
LOG_DIR = ROOT / "logs"
AUDIO_DIR = ROOT / "data" / "audio"
PDF_PATH = ROOT / "data" / "pdfs" / "reasons-and-persons.pdf"
KEYRING_SERVICE = "rabbit_hole"

# Episode identity. file_episode() names the file "{topic} - {title}{ext}" and
# tag_audio() embeds the title; the Plex `date` tag is now the generation day
# (set automatically by file_episode), no longer the source's year.
TITLE = "Reasons and Persons (Parfit)"
TOPIC_BUCKET = "Philosophy"
PUBLISHED = datetime(1984, 1, 1, tzinfo=timezone.utc)  # required by the item; no longer feeds the date tag
LANGUAGE = "en"
AUDIO_FORMAT = "deep_dive"
AUDIO_LENGTH = "long"
FOCUS = (
    "A long, comprehensive deep dive into Derek Parfit's book Reasons and Persons. "
    "Cover the major ideas: how self-interest and common-sense morality can be "
    "self-defeating; the reductionist view of personal identity (the teletransporter, "
    "and why what matters in survival is psychological continuity rather than a deep "
    "further fact); and population ethics (the Non-Identity Problem and the Repugnant "
    "Conclusion). Weave them together with the book's thread on rationality and time: "
    "our bias toward the future, why we feel 'thank goodness that's over' about past "
    "suffering yet dread equivalent future pain, and what that asymmetry implies for "
    "regret, relief, and how we should care about our lives across time. Use Parfit's "
    "thought experiments to make the ideas vivid; keep a thoughtful, exploratory tone."
)


def _build_paper() -> Paper:
    """A minimal Paper standing in for the book, with just the fields the back end
    reads (title/authors/published_at for tagging, pdf_path/language for generate)."""
    item = FeedItem(
        source_name="manual-book",
        source_weight=1.0,
        title=TITLE,
        summary="One-off long-form episode from a user-provided book PDF.",
        link="",
        published_at=PUBLISHED,
    )
    return Paper(
        doi=None,
        title=TITLE,
        authors=["Derek Parfit"],
        abstract=item.summary,
        published_at=PUBLISHED,
        pdf_path=PDF_PATH,
        language=LANGUAGE,
        source=item,
    )


def _preflight() -> list[str]:
    """Return human-readable blockers; empty list means good to go. Catches the
    cheap-to-detect failures before we commit ~40 min of NotebookLM time."""
    problems: list[str] = []
    if not PDF_PATH.exists():
        problems.append(f"book PDF not found at {PDF_PATH}")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} not on PATH (needed for the +40% loudness boost)")
    if not keyring.get_password(KEYRING_SERVICE, "plex_token"):
        problems.append("Plex token missing from keyring (service 'rabbit_hole', key 'plex_token')")
    # Functional nlm check: proves the CLI is present AND the login is still valid,
    # which is the failure that would otherwise surface only at notebook-create.
    try:
        r = subprocess.run(
            ["nlm", "list", "notebooks", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if r.returncode != 0:
            problems.append("nlm CLI failed `list notebooks` -- not authenticated? (try: nlm login)")
    except (OSError, subprocess.TimeoutExpired):
        problems.append("nlm CLI not runnable on PATH")
    return problems


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    logger = configure_logging(LOG_DIR, None)
    webhook = keyring.get_password(KEYRING_SERVICE, "discord_webhook_url")

    problems = _preflight()
    if problems:
        for p in problems:
            logger.error("book_episode_preflight_problem", problem=p)
        print("PREFLIGHT FAILED:")
        for p in problems:
            print("  -", p)
        return 2
    logger.info("book_episode_preflight_ok", pdf=str(PDF_PATH))
    print("Preflight OK.")
    if check_only:
        print("--check: stopping before generation (no NotebookLM time spent).")
        return 0

    cfg = load_config(CONFIG_PATH)
    token = keyring.get_password(KEYRING_SERVICE, "plex_token") or ""
    paper = _build_paper()

    logger.info("book_episode_start", title=TITLE, length=AUDIO_LENGTH, fmt=AUDIO_FORMAT)
    try:
        ep = generate(paper, cfg.notebooklm, out_dir=AUDIO_DIR,
                      audio_format=AUDIO_FORMAT, audio_length=AUDIO_LENGTH, focus=FOCUS)
    except NotebookLMError as e:
        logger.error("book_episode_nlm_failed", title=TITLE, error=str(e))
        post_discord(webhook, f"Rabbit Hole book episode FAILED at NotebookLM: {e}")
        return 1
    logger.info("book_episode_generated", audio=str(ep.audio_path))

    # Mirror _publish: a boost failure files the quieter episode rather than dropping it.
    try:
        boost_loudness(ep.audio_path, gain=cfg.audio.gain_factor)
        logger.info("book_episode_boosted", gain=cfg.audio.gain_factor)
    except AudioBoostError as e:
        logger.error("book_episode_boost_failed", title=TITLE, error=str(e))

    dest = file_episode(ep, TOPIC_BUCKET, cfg.plex, token)
    logger.info("book_episode_filed", path=str(dest))
    post_discord(webhook, f"Rabbit Hole book episode ready: {TITLE}\n{dest.name}")
    print("DONE:", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
