"""One-off: build a themed episode from a curated reading list (many papers and
articles) instead of a single paper, and file it into Plex like a routine episode.

Built 2026-07-27 for "The algorithm doesn't read your taste - it writes it", an
episode on how recommender systems shape the people who use them.

It bypasses the RSS -> resolve -> rank front of the pipeline and reuses the back
end unchanged:  generate(sources=[...]) -> boost_loudness(+40%) -> file_episode.
No RSS state is touched, so this can't collide with the weekly run's dedup or
recovery. The only new capability it leans on is notebooklm.generate's `sources`
override, which feeds the notebook a whole list of PDFs and URLs.

Modes:
  python scripts/make_topic_episode.py --fetch --check   # download sources, preflight
  python scripts/make_topic_episode.py --check           # preflight only, no NLM time spent
  python scripts/make_topic_episode.py                   # full generate + boost + file

The real run is meant to be launched DETACHED (scripts/run_topic_episode_detached.ps1)
so closing the terminal / Claude session can't kill it mid-generation.
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

import httpx  # noqa: E402
import keyring  # noqa: E402

from rabbit_hole.audio import AudioBoostError, boost_loudness  # noqa: E402
from rabbit_hole.config import load_config  # noqa: E402
from rabbit_hole.logging_config import configure_logging, post_discord  # noqa: E402
from rabbit_hole.notebooklm import NotebookLMError, SourceRef, generate  # noqa: E402
from rabbit_hole.plex import file_episode  # noqa: E402
from rabbit_hole.resolver import Paper  # noqa: E402
from rabbit_hole.sources import FeedItem  # noqa: E402

CONFIG_PATH = ROOT / "config" / "config.toml"
LOG_DIR = ROOT / "logs"
AUDIO_DIR = ROOT / "data" / "audio"
PDF_DIR = ROOT / "data" / "pdfs" / "algorithms-episode"
KEYRING_SERVICE = "rabbit_hole"

# Episode identity. file_episode() names the file "{topic} - {title}{ext}" and
# tag_audio() embeds the title; the Plex `date` tag is the generation day.
TITLE = "The algorithm doesn't read your taste — it writes it"
TOPIC_BUCKET = "Technology and society"
LANGUAGE = "en"
AUDIO_FORMAT = "deep_dive"
AUDIO_LENGTH = "long"

# Peer-reviewed papers and working papers, downloaded locally so the run doesn't
# depend on a publisher being reachable (or bot-blocking) at generate time. The
# origin URL is kept beside each filename because data/ is gitignored: without it
# the reading list would not be reproducible from the repo. `--fetch` re-downloads
# whatever is missing.
PDF_SOURCES = [
    ("01-survey-impacts-of-recommender-systems.pdf",
     "https://arxiv.org/pdf/2407.01630"),
    ("02-recognising-the-importance-of-preference-change.pdf",
     "https://arxiv.org/pdf/2203.10525"),
    ("03-estimating-and-penalizing-induced-preference-shifts.pdf",
     "https://arxiv.org/pdf/2204.11966"),
    ("04-when-for-you-isnt-for-you-tiktok-user-agency.pdf",
     "https://arxiv.org/pdf/2605.10690"),
    ("05-personalization-factors-on-tiktok.pdf",
     "https://arxiv.org/pdf/2201.12271"),
    ("06-revisiting-algorithmic-audits-of-tiktok.pdf",
     "https://arxiv.org/pdf/2504.18140"),
    ("07-follow-the-guides-human-vs-algorithmic-music-curation.pdf",
     "https://arxiv.org/pdf/2109.03915"),
    ("08-algorithmic-effects-on-diversity-of-consumption-spotify.pdf",
     "https://www.cs.toronto.edu/~ashton/pubs/alg-effects-spotify-www2020.pdf"),
    ("09-changing-their-tune-streaming-and-music-discovery.pdf",
     "https://thearf-org-unified-admin.s3.amazonaws.com/MSI/2020/06/MSI_Report_16-136_revised.pdf"),
]

# Sources with no stable open PDF: PMC full texts (publisher PDFs 403 scrapers)
# and two journalism/institutional pieces NotebookLM can fetch itself.
URL_SOURCES = [
    # Huszar et al., PNAS 2022 - algorithmic amplification of politics on Twitter
    "https://pmc.ncbi.nlm.nih.gov/articles/PMC8740571/",
    # Nature 2026 - the political effects of X's feed algorithm
    "https://pmc.ncbi.nlm.nih.gov/articles/PMC13061628/",
    # PNAS 2024 - Facebook/Instagram 2020 election deactivation experiment
    "https://pmc.ncbi.nlm.nih.gov/articles/PMC11126999/",
    # Nyhan et al., Nature 2023 - cutting like-minded content by a third changed
    # exposure but moved no attitudes. Stands in for its sibling Guess et al.
    # (Science 2023, the chronological-feed swap), which is paywalled.
    "https://pmc.ncbi.nlm.nih.gov/articles/PMC10396953/",
    # Rolling Stone - Discovery Mode as payola; the incentive layer under the
    # music recommendations.
    "https://www.rollingstone.com/pro/music-biz-commentary/"
    "spotify-payola-artist-rights-alliance-1170544/",
]

FOCUS = (
    "A long, comprehensive deep dive on how recommendation algorithms shape the "
    "people who use them. Build the episode around one claim: these systems do not "
    "merely predict taste, they help create it. Move through four movements. "
    "(1) The feedback loop: how engagement-optimized ranking and collaborative "
    "filtering work, and why a system trained on your clicks ends up shaping the "
    "clicks - induced preference shifts as an emergent optimum, not a conspiracy. "
    "(2) What the large field experiments actually found: amplification asymmetries, "
    "chronological-feed swaps that changed behavior a lot and attitudes surprisingly "
    "little, deactivation studies. Be honest about effect sizes; resist the scary "
    "narrative. (3) Who is steering the For You Page: which signals TikTok's feed "
    "really reads, why 'not interested' barely works, why rejected topics resurface - "
    "and why these audits are hard to reproduce. (4) The same machinery in music: "
    "evidence that algorithmic listening goes with narrower consumption, how "
    "editorial, algorithmic and organic curation differ, the counter-finding that "
    "streaming widened discovery, and Discovery Mode putting a price on algorithmic "
    "placement. Sit with the disagreements between the sources rather than resolving "
    "them. Thoughtful, curious, skeptical of both hype and dismissal."
)


def _sources() -> list[SourceRef]:
    refs = [SourceRef("file", str(PDF_DIR / name)) for name, _ in PDF_SOURCES]
    refs += [SourceRef("url", url) for url in URL_SOURCES]
    return refs


def _fetch_missing_pdfs() -> int:
    """Download any source PDF not already on disk. Returns the count fetched."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    fetched = 0
    with httpx.Client(follow_redirects=True, timeout=90) as client:
        for name, url in PDF_SOURCES:
            path = PDF_DIR / name
            if path.exists() and path.stat().st_size >= 10_000:
                continue
            print(f"fetching {name} <- {url}")
            r = client.get(url)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise RuntimeError(f"{url} did not return a PDF")
            path.write_bytes(r.content)
            fetched += 1
    return fetched


def _build_paper() -> Paper:
    """A minimal Paper standing in for the episode, with just the fields the back
    end reads (title for naming/tagging, language for generate). pdf_path is
    unused because generate() receives an explicit `sources` list."""
    published = datetime.now(timezone.utc)
    item = FeedItem(
        source_name="manual-topic",
        source_weight=1.0,
        title=TITLE,
        summary="One-off themed episode built from a curated reading list.",
        link="",
        published_at=published,
    )
    return Paper(
        doi=None,
        title=TITLE,
        authors=["various"],
        abstract=item.summary,
        published_at=published,
        pdf_path=PDF_DIR / PDF_SOURCES[0][0],
        language=LANGUAGE,
        source=item,
    )


def _url_reachable(url: str) -> bool:
    """HEAD, falling back to a ranged GET: some hosts (PMC, news sites) answer
    HEAD with 403/405 while serving GET fine."""
    try:
        with httpx.Client(follow_redirects=True, timeout=25) as client:
            r = client.head(url)
            if r.status_code >= 400:
                r = client.get(url, headers={"Range": "bytes=0-2048"})
            return r.status_code < 400
    except httpx.HTTPError:
        return False


def _preflight() -> list[str]:
    """Return human-readable blockers; empty list means good to go. Catches the
    cheap-to-detect failures before we commit ~40 min of NotebookLM time. With 14
    sources the reachability check matters more than in the single-PDF one-off:
    one dead link aborts the notebook after the sources ahead of it have ingested."""
    problems: list[str] = []
    # Build the Paper here rather than only on the real path: --check previously
    # reported OK while _build_paper() still raised, so the detached run died
    # seconds in with the preflight's blessing.
    try:
        _build_paper()
    except Exception as e:  # noqa: BLE001 - any construction failure is a blocker
        problems.append(f"episode metadata could not be built: {e!r}")
    for name, _ in PDF_SOURCES:
        path = PDF_DIR / name
        if not path.exists():
            problems.append(f"source PDF not found: {path}")
        elif path.stat().st_size < 10_000:
            problems.append(f"source PDF suspiciously small ({path.stat().st_size} B): {path}")
    for url in URL_SOURCES:
        if not _url_reachable(url):
            problems.append(f"source URL unreachable: {url}")
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
    args = sys.argv[1:]
    check_only = "--check" in args
    logger = configure_logging(LOG_DIR, None)
    webhook = keyring.get_password(KEYRING_SERVICE, "discord_webhook_url")
    sources = _sources()

    if "--fetch" in args:
        print(f"fetched {_fetch_missing_pdfs()} missing source PDF(s).")

    problems = _preflight()
    if problems:
        for p in problems:
            logger.error("topic_episode_preflight_problem", problem=p)
        print("PREFLIGHT FAILED:")
        for p in problems:
            print("  -", p)
        return 2
    logger.info("topic_episode_preflight_ok", sources=len(sources))
    print(f"Preflight OK ({len(sources)} sources: "
          f"{len(PDF_SOURCES)} PDFs + {len(URL_SOURCES)} URLs).")
    if check_only:
        print("--check: stopping before generation (no NotebookLM time spent).")
        return 0

    cfg = load_config(CONFIG_PATH)
    token = keyring.get_password(KEYRING_SERVICE, "plex_token") or ""
    paper = _build_paper()

    logger.info("topic_episode_start", title=TITLE, length=AUDIO_LENGTH,
                fmt=AUDIO_FORMAT, sources=len(sources))
    try:
        ep = generate(paper, cfg.notebooklm, out_dir=AUDIO_DIR,
                      audio_format=AUDIO_FORMAT, audio_length=AUDIO_LENGTH,
                      focus=FOCUS, sources=sources)
    except NotebookLMError as e:
        logger.error("topic_episode_nlm_failed", title=TITLE, error=str(e))
        post_discord(webhook, f"Rabbit Hole topic episode FAILED at NotebookLM: {e}")
        return 1
    logger.info("topic_episode_generated", audio=str(ep.audio_path))

    # Mirror _publish: a boost failure files the quieter episode rather than dropping it.
    try:
        boost_loudness(ep.audio_path, gain=cfg.audio.gain_factor)
        logger.info("topic_episode_boosted", gain=cfg.audio.gain_factor)
    except AudioBoostError as e:
        logger.error("topic_episode_boost_failed", title=TITLE, error=str(e))

    dest = file_episode(ep, TOPIC_BUCKET, cfg.plex, token)
    logger.info("topic_episode_filed", path=str(dest))
    post_discord(webhook, f"Rabbit Hole topic episode ready: {TITLE}\n{dest.name}")
    # The title carries an em dash, and a Scheduled Task's stdout is not UTF-8;
    # the episode is already filed by here, so never let printing be what fails.
    print("DONE:", str(dest).encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
