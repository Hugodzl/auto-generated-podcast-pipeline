"""Shared machinery for one-off / themed episodes.

A themed episode bypasses the RSS -> resolve -> rank front of the pipeline and
reuses the back end unchanged:

    fetch sources -> preflight -> generate(sources=[...]) -> boost_loudness -> file_episode

No RSS state is touched, so a one-off can't collide with the weekly run's dedup
or recovery.

WHY THIS MODULE EXISTS. This code lived as hand-copied blocks in four scripts
under scripts/, none of them under test, and the copies drifted:

  - `0984ff8` (build the Paper inside the preflight, so --check can't bless a run
    that dies seconds later) landed in the topic script only.
  - `0ae5913` (replace the HTTP reachability check with a probe that asks nlm
    itself) landed in the ai-sources script only, leaving the topic script
    carrying the known-wrong oracle for three weeks.
  - the browser User-Agent for publisher PDF hosts landed in the knowledge-system
    script only.

Each episode is now a small `EpisodeSpec` of data, and this module owns the
behaviour. Add an episode by writing a spec; fix a bug once.
"""
import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import keyring

from rabbit_hole.audio import AudioBoostError, boost_loudness
from rabbit_hole.config import load_config
from rabbit_hole.logging_config import configure_logging, post_discord
from rabbit_hole.notebooklm import NotebookLMError, SourceRef, generate
from rabbit_hole.plex import file_episode
from rabbit_hole.resolver import Paper
from rabbit_hole.sources import FeedItem

# The repo root, from the installed package location. Episode scripts live in
# scripts/ but the data, config and logs all hang off the root.
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.toml"
LOG_DIR = ROOT / "logs"
AUDIO_DIR = ROOT / "data" / "audio"
KEYRING_SERVICE = "rabbit_hole"

# A PDF smaller than this is an error page someone saved, not a paper.
MIN_PDF_BYTES = 10_000

# nature.com and frontiersin.org serve a real PDF to curl and an HTML
# interstitial to httpx's default agent, which surfaces as "did not return a
# PDF". This header is load-bearing; test_fetch_sends_a_browser_user_agent pins it.
_PDF_FETCH_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/pdf,*/*",
}


@dataclass(frozen=True)
class EpisodeSpec:
    """Everything that is genuinely per-episode. Everything else is behaviour.

    `slug` namespaces the structured log events (`{slug}_episode_filed`), so it
    should be a stable identifier; `label` is the human-readable name used in
    Discord notifications.
    """
    slug: str
    title: str
    topic_bucket: str
    pdf_dir: Path
    focus: str
    pdf_sources: tuple[tuple[str, str], ...] = ()   # (filename, origin url)
    url_sources: tuple[str, ...] = ()
    label: str = ""
    language: str = "en"
    audio_format: str = "deep_dive"
    audio_length: str = "long"

    def __post_init__(self):
        if not self.label:
            object.__setattr__(self, "label", self.slug.replace("_", "-"))
        object.__setattr__(self, "pdf_dir", Path(self.pdf_dir))
        object.__setattr__(self, "pdf_sources", tuple(self.pdf_sources))
        object.__setattr__(self, "url_sources", tuple(self.url_sources))

    def event(self, name: str) -> str:
        return f"{self.slug}_episode_{name}"


def sources(spec: EpisodeSpec) -> list[SourceRef]:
    """Local PDFs first, then URLs. Order is the order nlm ingests them, which is
    also the order a mid-run failure reports, so keep it stable."""
    refs = [SourceRef("file", str(spec.pdf_dir / name)) for name, _ in spec.pdf_sources]
    refs += [SourceRef("url", url) for url in spec.url_sources]
    return refs


def build_paper(spec: EpisodeSpec) -> Paper:
    """A minimal Paper standing in for the episode, carrying just the fields the
    back end reads: title for naming/tagging, language for generation. pdf_path is
    unused at generate time because `sources` is passed explicitly, but plex
    tagging still expects a Paper, so it points at the first source PDF."""
    if not spec.pdf_sources:
        raise ValueError(
            f"{spec.slug}: at least one PDF source is required "
            "(pdf_path stands in for the episode during tagging)")
    published = datetime.now(timezone.utc)
    item = FeedItem(
        source_name="manual-topic",
        source_weight=1.0,
        title=spec.title,
        summary="One-off themed episode built from a curated reading list.",
        link="",
        published_at=published,
    )
    return Paper(
        doi=None,
        title=spec.title,
        authors=["various"],
        abstract=item.summary,
        published_at=published,
        pdf_path=spec.pdf_dir / spec.pdf_sources[0][0],
        language=spec.language,
        source=item,
    )


def fetch_missing_pdfs(spec: EpisodeSpec) -> int:
    """Download any source PDF not already on disk. Returns the count fetched.

    The origin URL lives beside each filename in the spec because data/ is
    gitignored: without it the reading list would not be reproducible from the
    repo."""
    spec.pdf_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    with httpx.Client(follow_redirects=True, timeout=180,
                      headers=_PDF_FETCH_HEADERS) as client:
        for name, url in spec.pdf_sources:
            path = spec.pdf_dir / name
            if path.exists() and path.stat().st_size >= MIN_PDF_BYTES:
                continue
            print(f"fetching {name} <- {url}")
            r = client.get(url)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise RuntimeError(f"{url} did not return a PDF")
            path.write_bytes(r.content)
            fetched += 1
    return fetched


def _nlm(args: list[str], timeout: float):
    """Run one nlm subcommand; None if the binary is missing or the call times out."""
    try:
        return subprocess.run(
            ["nlm", *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _probe_title() -> str:
    return f"rh-preflight {uuid.uuid4().hex[:8]}"


def _find_notebook_id(title: str) -> str | None:
    r = _nlm(["list", "notebooks", "--json"], 120)
    if r is None or r.returncode != 0:
        return None
    try:
        notebooks = json.loads(r.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(notebooks, list):
        return None
    matches = [n for n in notebooks if n.get("title") == title and n.get("id")]
    matches.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    return matches[0]["id"] if matches else None


def urls_nlm_can_ingest(urls: list[str]) -> tuple[list[str], str | None]:
    """Ask nlm itself which URL sources it can actually ingest, in one throwaway
    notebook. Returns (rejected urls, fatal error or None).

    An HTTP reachability check is the wrong oracle here, and wrong in BOTH
    directions -- which is the entire reason this exists:
      - digitalcontentnext.org answers 200 to httpx and `nlm source add` refuses
        it, which killed a real run four minutes in after 18 of 22 sources had
        already ingested (2026-08-11).
      - cjr.org, bls.gov, tandfonline.com and lsa.umich.edu all answer 403 to a
        plain client and NotebookLM ingests them fine.
      - theconversation.com and cdnsciencepub.com answer 200 and nlm refuses both
        (2026-08-31).
    robots.txt is not the predictor either: thebulletin.org disallows GPTBot,
    ClaudeBot and Google-Extended outright and still ingests fine, because a
    user-initiated NotebookLM fetch is not one of those crawlers.

    Costs one notebook and ~10s per URL, spends no audio-generation time, and
    turns a class of mid-run aborts into a preflight failure. The notebook is
    deleted in a finally: an aborted probe otherwise litters the account."""
    title = _probe_title()
    created = _nlm(["notebook", "create", title], 120)
    if created is None or created.returncode != 0:
        return [], "could not create the preflight probe notebook (nlm auth?)"
    notebook_id = _find_notebook_id(title)
    if not notebook_id:
        return [], "preflight probe notebook created but its id was not found"
    rejected: list[str] = []
    try:
        for url in urls:
            added = _nlm(["source", "add", notebook_id, "--url", url,
                          "--wait", "--wait-timeout", "120"], 200)
            if added is None or added.returncode != 0:
                rejected.append(url)
    finally:
        _nlm(["notebook", "delete", notebook_id, "--confirm"], 120)
    return rejected, None


def preflight(spec: EpisodeSpec, probe_urls: bool = True) -> list[str]:
    """Return human-readable blockers; empty list means good to go.

    Catches the cheap-to-detect failures before committing ~40 min of NotebookLM
    time. With a long reading list the URL check matters most: one source nlm
    won't take aborts the whole notebook after everything ahead of it has already
    ingested.

    Cheap local checks run first; the nlm URL probe runs last and only if the CLI
    is authenticated, since without auth every probe would fail anyway and bury
    the real cause. Pass probe_urls=False for a local-only sanity check."""
    problems: list[str] = []
    # Build the Paper here rather than only on the real path: --check once
    # reported OK while build_paper() still raised, so a detached run died
    # seconds in with the preflight's blessing.
    try:
        build_paper(spec)
    except Exception as e:  # noqa: BLE001 - any construction failure is a blocker
        problems.append(f"episode metadata could not be built: {e!r}")
    for name, _ in spec.pdf_sources:
        path = spec.pdf_dir / name
        if not path.exists():
            problems.append(f"source PDF not found: {path}")
        elif path.stat().st_size < MIN_PDF_BYTES:
            problems.append(
                f"source PDF suspiciously small ({path.stat().st_size} B): {path}")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} not on PATH (needed for the loudness boost)")
    if not keyring.get_password(KEYRING_SERVICE, "plex_token"):
        problems.append(
            "Plex token missing from keyring (service 'rabbit_hole', key 'plex_token')")
    # Functional nlm check: proves the CLI is present AND the login is still
    # valid, which is the failure that would otherwise surface at notebook-create.
    r = _nlm(["list", "notebooks", "--json"], 60)
    if r is None:
        problems.append("nlm CLI not runnable on PATH")
        return problems
    if r.returncode != 0:
        problems.append(
            "nlm CLI failed `list notebooks` -- not authenticated? (try: nlm login)")
        return problems

    if probe_urls and spec.url_sources:
        rejected, fatal = urls_nlm_can_ingest(list(spec.url_sources))
        if fatal:
            problems.append(fatal)
        for url in rejected:
            problems.append(f"nlm refuses to ingest this URL source (swap it): {url}")
    return problems


def run(spec: EpisodeSpec, argv: list[str]) -> int:
    """Build one themed episode end to end. Exit codes: 0 ok, 1 NotebookLM
    failed, 2 preflight refused to start.

    Flags: --fetch (download missing source PDFs first), --check (stop after the
    preflight, spending no NotebookLM time), --no-probe (skip the nlm URL probe).
    """
    check_only = "--check" in argv
    logger = configure_logging(LOG_DIR, None)
    webhook = keyring.get_password(KEYRING_SERVICE, "discord_webhook_url")
    refs = sources(spec)

    if "--fetch" in argv:
        print(f"fetched {fetch_missing_pdfs(spec)} missing source PDF(s).")

    problems = preflight(spec, probe_urls="--no-probe" not in argv)
    if problems:
        for p in problems:
            logger.error(spec.event("preflight_problem"), problem=p)
        print("PREFLIGHT FAILED:")
        for p in problems:
            print("  -", p)
        return 2
    logger.info(spec.event("preflight_ok"), sources=len(refs))
    print(f"Preflight OK ({len(refs)} sources: "
          f"{len(spec.pdf_sources)} PDFs + {len(spec.url_sources)} URLs).")
    if check_only:
        print("--check: stopping before generation (no NotebookLM time spent).")
        return 0

    cfg = load_config(CONFIG_PATH)
    token = keyring.get_password(KEYRING_SERVICE, "plex_token") or ""
    paper = build_paper(spec)

    logger.info(spec.event("start"), title=spec.title, length=spec.audio_length,
                fmt=spec.audio_format, sources=len(refs))
    try:
        ep = generate(paper, cfg.notebooklm, out_dir=AUDIO_DIR,
                      audio_format=spec.audio_format, audio_length=spec.audio_length,
                      focus=spec.focus, sources=refs)
    except NotebookLMError as e:
        logger.error(spec.event("nlm_failed"), title=spec.title, error=str(e))
        post_discord(webhook,
                     f"Rabbit Hole {spec.label} episode FAILED at NotebookLM: {e}")
        return 1
    logger.info(spec.event("generated"), audio=str(ep.audio_path))

    # Mirror _publish: a boost failure files the quieter episode rather than
    # dropping it.
    try:
        boost_loudness(ep.audio_path, gain=cfg.audio.gain_factor)
        logger.info(spec.event("boosted"), gain=cfg.audio.gain_factor)
    except AudioBoostError as e:
        logger.error(spec.event("boost_failed"), title=spec.title, error=str(e))

    dest = file_episode(ep, spec.topic_bucket, cfg.plex, token)
    logger.info(spec.event("filed"), path=str(dest))
    post_discord(webhook,
                 f"Rabbit Hole {spec.label} episode ready: {spec.title}\n{dest.name}")
    # The titles carry em dashes and a Scheduled Task's stdout is not UTF-8; the
    # episode is already filed by here, so never let printing be what fails.
    print("DONE:", str(dest).encode("ascii", "replace").decode("ascii"))
    return 0
