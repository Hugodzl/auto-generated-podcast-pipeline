"""One-off: build a themed episode around Nicolas Michaelsen's essay "Death of a
Knowledge System" (Ecologies of Wisdom, 2025-06-19) - the claim that Western
civilisation's propositional-knowledge system is collapsing as AI automates it,
and that embodied, relational and place-based knowing is what survives.

Built 2026-08-31. Same shape as make_ai_sources_episode.py: it bypasses the
RSS -> resolve -> rank front of the pipeline and reuses the back end unchanged:
generate(sources=[...]) -> boost_loudness(+40%) -> file_episode. No RSS state is
touched, so this can't collide with the weekly run's dedup or recovery.

Editorial note on sourcing: a search for direct responses to the essay found none
worth the name - the only third-party items are a Facebook book-note and the
author's own republication. So the "what do trustworthy sources say" half of this
episode is built from the literature the essay's claims actually live in: labour
economics on AI exposure vs displacement, the cognitive-science lineage behind the
four-ways-of-knowing model, and the internal critiques of degrowth/bioregionalism
and of Two-Eyed Seeing. Several of those sources disagree with the essay, and a
few disagree with each other; that is the point.

Modes:
  python scripts/make_knowledge_system_episode.py --fetch --check   # download sources, preflight
  python scripts/make_knowledge_system_episode.py --check           # preflight only, no NLM time spent
  python scripts/make_knowledge_system_episode.py --check --no-probe # skip the ~2 min nlm URL probe
  python scripts/make_knowledge_system_episode.py                   # full generate + boost + file

The preflight asks nlm itself whether it can ingest each URL source, because HTTP
reachability turned out to be the wrong oracle in both directions - see
_urls_nlm_can_ingest. It earned its keep again here: bls.gov, tandfonline and
lsa.umich.edu all answer 403 to a plain client and nlm ingests them fine, while
theconversation.com and cdnsciencepub answer 200 and nlm refuses both - so those
two were swapped for probed equivalents rather than discovered mid-run.

The real run is meant to be launched DETACHED
(scripts/run_knowledge_system_episode_detached.ps1) so closing the terminal /
Claude session can't kill it mid-generation.
"""
import json
import subprocess
import sys
import uuid
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
PDF_DIR = ROOT / "data" / "pdfs" / "knowledge-system-episode"
KEYRING_SERVICE = "rabbit_hole"

# Episode identity. file_episode() names the file "{topic} - {title}{ext}" and
# tag_audio() embeds the title; the Plex `date` tag is the generation day.
# The title is the episode's own finding, not the essay's: the measured damage is
# concentrated at the entry point to knowledge work, not across it.
TITLE = "The knowledge system isn't dying — its front door is"
TOPIC_BUCKET = "Technology and society"
LANGUAGE = "en"
AUDIO_FORMAT = "deep_dive"
AUDIO_LENGTH = "long"

# Papers and reports downloaded locally so the run doesn't depend on a publisher
# being reachable (or bot-blocking) at generate time. The origin URL is kept beside
# each filename because data/ is gitignored: without it the reading list would not
# be reproducible from the repo. `--fetch` re-downloads what's missing. Every URL
# below was confirmed to return a real PDF on 2026-08-31.
#
# Numbering follows the episode's five movements: 01-02 the scale of exposure,
# 03-08 what has actually happened to cognitive work, 09-12 the ways-of-knowing
# model and what outsourcing propositional work does to a person, 13-14 whether
# expertise is being extended or commodified, 15 Indigenous epistemology on its
# own terms.
# Papers and reports downloaded locally so the run doesn't depend on a publisher
# being reachable (or bot-blocking) at generate time. The origin URL is kept beside
# each filename because data/ is gitignored: without it the reading list would not
# be reproducible from the repo. `--fetch` re-downloads what's missing. Every URL
# below was confirmed to return a real PDF on 2026-08-31.
#
# DELIBERATELY SHORT. The first build of this episode carried 28 sources and was
# killed before generation, because the length record says a longer reading list buys
# a SHORTER episode, not a longer one: 1 PDF -> 46 min, 14 sources -> 41 min, 22
# sources -> 21.9 min. The brief asked for a long episode, so the list is trimmed to
# 14 and the focus is tighter. Cut on that pass, and worth restoring only if that
# length relationship turns out to be noise: the IMF staff note (redundant with the
# ILO paper), Noy & Zhang on writing (redundant with Generative AI at Work),
# Dell'Acqua's cybernetic-teammate experiments, the MIT "Your Brain on ChatGPT"
# cognitive-debt preprint plus the Marrone & Kovanovic critique of it, the
# Microsoft/CMU critical-thinking survey, Autor 2014 on Polanyi's paradox, the Nature
# "democratization dilemma" paper, Smith et al. on Two-Eyed Seeing, and Michaelsen's
# own "Birth of the Wisdom Economy". All 15 originally-fetched PDFs are still in
# data/pdfs/knowledge-system-episode/ if a follow-up episode wants them.
#
# Filenames keep their original numbers so they still match what is on disk; the
# order below is the episode's order, not the filenames'.
PDF_SOURCES = [
    # --- The strongest evidence FOR the essay ---
    # Brynjolfsson, Chandar & Chen (Stanford Digital Economy Lab), Aug 2026 revision.
    # THE anchor for the episode's title: a 19% relative employment gap for 22-25
    # year olds in AI-exposed occupations, no comparable gap for experienced workers,
    # no evidence of widespread economy-wide displacement, and the channel is
    # reduced hiring rather than increased separations.
    ("03-canaries-in-the-coal-mine-ai-employment-effects.pdf",
     "https://digitaleconomy.stanford.edu/app/uploads/2026/08/Canaries_August2026.pdf"),

    # --- The evidence against a system-wide collapse ---
    # Acemoglu 2024 - the macro skeptic. Non-trivial but modest: no more than ~0.66%
    # TFP over ten years, and he argues even that is probably too generous.
    ("04-acemoglu-simple-macroeconomics-of-ai.pdf",
     "https://economics.mit.edu/sites/default/files/2024-05/"
     "The%20Simple%20Macroeconomics%20of%20AI.pdf"),
    # METR RCT, 2025 - 16 experienced open-source developers, 246 real tasks: AI made
    # them 19% SLOWER, and they still believed afterwards it had made them 20% faster.
    # The single best evidence that self-report about AI capability is unreliable -
    # which matters because the essay, like most of this debate, runs on impressions.
    ("05-metr-early-2025-ai-developer-productivity.pdf",
     "https://arxiv.org/pdf/2507.09089"),
    # ILO working paper, 2023 - the counterweight to "300 million jobs gone":
    # augmentation dominates automation almost everywhere, clerical work is the
    # standout exception, and the effect is wildly uneven by country income.
    ("02-ilo-generative-ai-and-jobs-global-analysis.pdf",
     "https://www.ilo.org/wcmsp5/groups/public/---dgreports/---inst/documents/"
     "publication/wcms_890761.pdf"),
    # Brynjolfsson, Li & Raymond - 5,000 customer-support agents: ~14% average
    # productivity gain, concentrated almost entirely in the least experienced
    # workers. Sits in productive tension with the Canaries result: the people AI
    # helps most are the people being hired least.
    ("06-generative-ai-at-work-customer-support.pdf",
     "https://www.nber.org/system/files/working_papers/w31161/w31161.pdf"),

    # --- The model of knowing the essay runs on ---
    # An OSF revision of Vervaeke's 4P model - useful precisely because it is a
    # friendly critique from inside: it argues the four-way split needs reworking.
    # This is the essay's core theoretical scaffolding, held to account.
    ("09-vervaeke-4p-model-revised-four-ways-of-knowing.pdf",
     "https://osf.io/download/gw7r5/"),

    # --- The rival account ---
    # Autor 2024 - the strongest alternative to the essay: the same technology can
    # extend the reach of human expertise to more workers, or strip it out, and which
    # one happens is decided by institutions and design, not by metaphysics.
    ("13-autor-applying-ai-to-rebuild-middle-class-jobs.pdf",
     "https://www.nber.org/system/files/working_papers/w32140/w32140.pdf"),

    # --- Indigenous epistemology, on its own terms ---
    # Frontiers in Sociology, 2022 - an Indigenous critique of a Western discipline,
    # written from inside. Included so the episode engages Yunkaporta's tradition as a
    # live intellectual position rather than as a supplement to Western knowing.
    ("15-an-indigenous-critique-expanding-sociology.pdf",
     "https://www.frontiersin.org/journals/sociology/articles/10.3389/"
     "fsoc.2022.1047812/pdf"),
]

# The essay itself, plus the reporting and measurement with no stable open PDF.
# Three of these answer 403 to a plain HTTP client and are kept anyway: the nlm probe
# in _preflight is the only oracle that matters (see the module docstring).
URL_SOURCES = [
    # The subject of the episode. Everything else is here to test it.
    "https://ecologiesofwisdom.substack.com/p/death-of-a-knowledge-system",
    # The actual Goldman Sachs note behind "300 million jobs" - which says exposure,
    # projects ~25% of tasks automatable and only ~7% of workers fully displaced,
    # and forecasts a 7% GDP increase. The essay cites the headline, not the note.
    "https://www.goldmansachs.com/insights/articles/"
    "generative-ai-could-raise-global-gdp-by-7-percent",
    # KPMG's care-economy mapping - and the sting in the tail: turnover near 80%,
    # weak ladders, structurally constrained wages. Growing in demand is not the same
    # as growing in value, which is the essay's biggest unexamined leap.
    "https://kpmg.com/us/en/articles/2026/march-2026-mapping-the-care-economy-in-2026.html",
    # Construction Physics on robot dexterity - the empirical check on "the hand is
    # safe". Humanoids are improving and are still nowhere near human manipulation.
    "https://www.construction-physics.com/p/robot-dexterity-still-seems-hard",
    # Planning Theory & Practice, 2025 - bioregionalism and degrowth from inside the
    # degrowth literature, including the finding that bioregionalism implies UPscaling
    # planning, and that a self-sufficient bioregion can't deliver high living standards.
    "https://www.tandfonline.com/doi/full/10.1080/14649357.2025.2524106",
    # University of Michigan Psychology on what dopamine actually does - wanting, not
    # liking; prediction error, not certainty. The essay's weakest paragraph needs this.
    "https://lsa.umich.edu/psych/news-events/all-news/faculty-news/dopamine--explained.html",
]

FOCUS = (
    "A long, unhurried deep dive built around one essay: Nicolas Michaelsen's 'Death "
    "of a Knowledge System' (Ecologies of Wisdom, June 2025). Its argument: the West "
    "built a knowledge system that treated propositional knowing - knowing THAT things "
    "are true, abstract, credentialed, disembodied - as the only real knowledge and the "
    "main measure of a person's worth; AI is now automating exactly that; and what "
    "survives is procedural, perspectival and participatory knowing, rooted in craft, "
    "care, relationship and place.\n\n"
    "Take the essay seriously - it asks a better question than most AI commentary, "
    "which is what kind of knowing was being paid for in the first place. But this is "
    "claim-checking, not cheerleading and not debunking. Say once, near the top, that "
    "no substantive published rebuttal to this essay exists, so it has to be tested "
    "against the literature its claims live in - and several of those sources disagree "
    "with it and with each other. Always give the real number and who measured it, and "
    "flag when something is a single study, a projection, a bank's forecast, or a "
    "framework rather than a finding. Take your time: go deep on each of four "
    "movements rather than hurrying through a list.\n\n"
    "(1) THE NUMBER THAT CARRIES THE ARGUMENT. The essay's rhetorical engine is '300 "
    "million jobs'. Go to the source: Goldman Sachs, March 2023, where the word is "
    "EXPOSED, not lost - the same note projects roughly a quarter of work tasks "
    "automatable, only around 7% of workers fully displaced, and a 7% rise in global "
    "GDP. Then the ILO's global analysis, which finds augmentation dominating "
    "automation nearly everywhere, clerical work as the standout exception, and the "
    "burden landing very unevenly by country income. Be fair: a misquoted statistic "
    "does not kill the underlying worry, and the essay's other figures - $1.7 trillion "
    "in US student debt, a billion-ish knowledge workers, a multi-trillion education "
    "industry - are roughly right even where they are estimates rather than "
    "measurements. But notice what the correction does to the story: exposure spread "
    "thinly across many jobs is a different world from a system dying.\n\n"
    "(2) SO WHAT IS ACTUALLY BREAKING? This is the movement that decides the episode, "
    "and the answer is sharper than either yes or no. The strongest evidence FOR the "
    "essay: employment of 22-to-25-year-olds in AI-exposed occupations sits about 19% "
    "below where it would be had it tracked their less-exposed peers, experienced "
    "workers show no comparable gap, and the mechanism is reduced hiring rather than "
    "firing - while the same authors are explicit that they find no widespread "
    "economy-wide displacement. Now the evidence against a collapse. Acemoglu's macro "
    "estimate: total factor productivity gains of at most about 0.66% over ten years, "
    "probably less. And the finding to sit with longest - a randomised trial where "
    "experienced developers were 19% SLOWER with AI on real tasks in code they knew "
    "well, and still believed afterwards it had sped them up by 20%. Dwell on that, "
    "because nearly every confident claim in this debate, the essay's included, rests "
    "on somebody's impression of what these systems can do. Then the knot: where the "
    "gains are real, in customer support, they land disproportionately on the LEAST "
    "experienced workers - so the people AI helps most are the people being hired "
    "least. Put those together and you get the episode's actual finding: what is "
    "measurably breaking is the entry ramp and the scarcity premium on having "
    "accumulated a lot of propositional knowledge - not the faculty, and not the whole "
    "edifice. The front door, not the house.\n\n"
    "(3) THE FOUR WAYS OF KNOWING - AND THE ONE PARAGRAPH THAT DOESN'T HOLD. The "
    "propositional / procedural / perspectival / participatory split comes from John "
    "Vervaeke, standing on Ryle's knowing-how versus knowing-that, Polanyi's tacit "
    "knowledge - we know more than we can tell - Gibson's affordances and Dreyfus on "
    "expertise. Explain it properly and generously, including participatory knowing as "
    "the hardest and most interesting of the four. Then be honest about its status: a "
    "well-motivated organising framework, not an empirical result, and one under "
    "active revision from inside its own literature - use that revision rather than "
    "treating the model as settled. Then take on the essay's weakest section directly: "
    "the claim that cognitive professionals are neurochemically addicted to the "
    "dopamine hit of articulating certainty, with withdrawal when it goes. That is a "
    "metaphor doing the work of a mechanism. Dopamine is not the pleasure chemical and "
    "not a certainty signal - the Berridge and Robinson line separates wanting from "
    "liking, and dopamine tracks wanting and reward prediction error. Be direct that "
    "the essay is worse for the paragraph and does not need it: having your identity "
    "fused to being the person who knows is real and painful, and it does not require "
    "a neurotransmitter to be true.\n\n"
    "(4) DOES THE HAND SAVE YOU? The essay's practical advice is to ground yourself in "
    "craft, care and place. Test it in three directions. On dexterity: humanoid robots "
    "are improving fast on demos and remain nowhere near human manipulation, with "
    "credible engineering estimates putting genuinely rugged industrial dexterity a "
    "decade out - so the hand is safer than the keyboard, but that is a lead, not a "
    "moat. On care: the essay's forecast is right about demand and probably wrong "
    "about value - care work is the fastest-growing category in the projections and "
    "still runs turnover near 80% on low pay with weak ladders. Explain why that does "
    "not self-correct - work whose productivity cannot rise gets squeezed by whoever "
    "is paying, usually a public budget - and land it as the sharpest correction the "
    "episode has: 'this work will grow in value' and 'this work will pay you more' are "
    "different sentences, and the last fifty years demonstrate the difference. On "
    "place: steelman the bioregional, cosmolocal programme, then bring the critique "
    "from inside the degrowth literature itself - the local scale is systematically "
    "over-weighted, a self-sufficient bioregion cannot deliver high living standards "
    "in an urbanised world, and bioregionalism in practice implies UPSCALING planning "
    "and inter-regional coordination rather than retreat to the small. Handle "
    "Yunkaporta's hand metaphor carefully: engage the Indigenous critique of Western "
    "disciplinary knowledge as a live intellectual position in its own right, and note "
    "the standing warning in that scholarship against treating Indigenous epistemology "
    "as a resource to be folded into a Western frame that keeps setting the terms - "
    "which is close to what the essay does, with good intentions. Then put the real "
    "rival account on the table: the same technology can extend the reach of human "
    "expertise to a wider set of workers or strip expertise out and commodify the "
    "labour, and which one happens is settled by institutions, licensing, firm design "
    "and policy - not by which mode of knowing is metaphysically deeper.\n\n"
    "Close by sorting the essay honestly. What survives: that a knowledge system is a "
    "real structure with a real payroll; that the entry ramp into cognitive work is "
    "visibly narrowing for the young; that an identity fused to knowing things is a "
    "genuine and under-discussed casualty; that procedural, perspectival and "
    "participatory knowing were undervalued long before AI showed up. What is "
    "overstated: death, collapse, and the timeline - the measured effects are "
    "concentrated, uneven, and mostly about hiring. What is simply wrong: the dopamine "
    "mechanism, and the slide from 'demand will grow' to 'you will be valued'. And "
    "what the essay is underrated for: seeing that the real question is not which jobs "
    "go, but which kind of knowing a society is willing to pay for - which is a "
    "political choice, and is still open. Sit with the disagreements between the "
    "sources rather than resolving them. Thoughtful, curious, warm toward the essay "
    "and unsentimental about its evidence."
)


def _sources() -> list[SourceRef]:
    refs = [SourceRef("file", str(PDF_DIR / name)) for name, _ in PDF_SOURCES]
    refs += [SourceRef("url", url) for url in URL_SOURCES]
    return refs


def _fetch_missing_pdfs() -> int:
    """Download any source PDF not already on disk. Returns the count fetched."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    fetched = 0
    # A browser User-Agent is load-bearing, not cargo cult: nature.com and
    # frontiersin.org both serve a real PDF to curl and an interstitial HTML page
    # to httpx's default agent, which surfaced here as "did not return a PDF".
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Accept": "application/pdf,*/*",
    }
    with httpx.Client(follow_redirects=True, timeout=180, headers=headers) as client:
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


def _urls_nlm_can_ingest(urls: list[str]) -> tuple[list[str], str | None]:
    """Ask nlm itself which URL sources it can actually ingest, in one throwaway
    notebook. Returns (rejected urls, fatal error or None).

    An HTTP reachability check is the wrong oracle here and wrong in BOTH
    directions, which is why this exists (both cases observed 2026-08-11):
      - cjr.org answers 403 to a plain httpx client, yet NotebookLM ingests it.
      - digitalcontentnext.org answers 200 to httpx, and `nlm source add` refuses
        it -- which killed a real run four minutes in, after 18 sources had
        already been ingested.
    robots.txt is not the predictor either: thebulletin.org disallows GPTBot,
    ClaudeBot and Google-Extended outright and still ingests fine, because a
    user-initiated NotebookLM fetch is not one of those crawlers.

    Costs one notebook and ~10s per URL, spends no audio-generation time, and
    turns a class of mid-run aborts into a preflight failure."""
    title = f"rh-preflight {uuid.uuid4().hex[:8]}"
    created = _nlm(["notebook", "create", title], 120)
    if created is None or created.returncode != 0:
        return [], "could not create the preflight probe notebook (nlm auth?)"
    notebook_id = _find_notebook_id_local(title)
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


def _nlm(args: list[str], timeout: float):
    """Run one nlm subcommand; None if the binary is missing or the call times out."""
    try:
        return subprocess.run(
            ["nlm", *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _find_notebook_id_local(title: str) -> str | None:
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


def _preflight(probe_urls: bool = True) -> list[str]:
    """Return human-readable blockers; empty list means good to go. Catches the
    cheap-to-detect failures before we commit ~40 min of NotebookLM time. With 14
    sources the URL check matters most: one source nlm won't take aborts the whole
    notebook after everything ahead of it has already ingested, which is exactly
    how the first run of the AI-sources episode died.

    Cheap local checks run first; the nlm URL probe runs last and only if the CLI
    is authenticated, since without auth every probe would fail anyway. Pass
    probe_urls=False to skip it (local-only sanity check)."""
    problems: list[str] = []
    # Build the Paper here rather than only on the real path: --check once reported
    # OK while _build_paper() still raised, so a detached run died seconds in with
    # the preflight's blessing.
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
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} not on PATH (needed for the +40% loudness boost)")
    if not keyring.get_password(KEYRING_SERVICE, "plex_token"):
        problems.append("Plex token missing from keyring (service 'rabbit_hole', key 'plex_token')")
    # Functional nlm check: proves the CLI is present AND the login is still valid,
    # which is the failure that would otherwise surface only at notebook-create.
    r = _nlm(["list", "notebooks", "--json"], 60)
    if r is None:
        problems.append("nlm CLI not runnable on PATH")
        return problems
    if r.returncode != 0:
        problems.append("nlm CLI failed `list notebooks` -- not authenticated? (try: nlm login)")
        return problems

    if probe_urls and URL_SOURCES:
        rejected, fatal = _urls_nlm_can_ingest(URL_SOURCES)
        if fatal:
            problems.append(fatal)
        for url in rejected:
            problems.append(f"nlm refuses to ingest this URL source (swap it): {url}")
    return problems


def main() -> int:
    args = sys.argv[1:]
    check_only = "--check" in args
    logger = configure_logging(LOG_DIR, None)
    webhook = keyring.get_password(KEYRING_SERVICE, "discord_webhook_url")
    sources = _sources()

    if "--fetch" in args:
        print(f"fetched {_fetch_missing_pdfs()} missing source PDF(s).")

    problems = _preflight(probe_urls="--no-probe" not in args)
    if problems:
        for p in problems:
            logger.error("knowledge_system_episode_preflight_problem", problem=p)
        print("PREFLIGHT FAILED:")
        for p in problems:
            print("  -", p)
        return 2
    logger.info("knowledge_system_episode_preflight_ok", sources=len(sources))
    print(f"Preflight OK ({len(sources)} sources: "
          f"{len(PDF_SOURCES)} PDFs + {len(URL_SOURCES)} URLs).")
    if check_only:
        print("--check: stopping before generation (no NotebookLM time spent).")
        return 0

    cfg = load_config(CONFIG_PATH)
    token = keyring.get_password(KEYRING_SERVICE, "plex_token") or ""
    paper = _build_paper()

    logger.info("knowledge_system_episode_start", title=TITLE, length=AUDIO_LENGTH,
                fmt=AUDIO_FORMAT, sources=len(sources))
    try:
        ep = generate(paper, cfg.notebooklm, out_dir=AUDIO_DIR,
                      audio_format=AUDIO_FORMAT, audio_length=AUDIO_LENGTH,
                      focus=FOCUS, sources=sources)
    except NotebookLMError as e:
        logger.error("knowledge_system_episode_nlm_failed", title=TITLE, error=str(e))
        post_discord(webhook, f"Rabbit Hole knowledge-system episode FAILED at NotebookLM: {e}")
        return 1
    logger.info("knowledge_system_episode_generated", audio=str(ep.audio_path))

    # Mirror _publish: a boost failure files the quieter episode rather than dropping it.
    try:
        boost_loudness(ep.audio_path, gain=cfg.audio.gain_factor)
        logger.info("knowledge_system_episode_boosted", gain=cfg.audio.gain_factor)
    except AudioBoostError as e:
        logger.error("knowledge_system_episode_boost_failed", title=TITLE, error=str(e))

    dest = file_episode(ep, TOPIC_BUCKET, cfg.plex, token)
    logger.info("knowledge_system_episode_filed", path=str(dest))
    post_discord(webhook, f"Rabbit Hole knowledge-system episode ready: {TITLE}\n{dest.name}")
    # The title carries an em dash, and a Scheduled Task's stdout is not UTF-8;
    # the episode is already filed by here, so never let printing be what fails.
    print("DONE:", str(dest).encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
