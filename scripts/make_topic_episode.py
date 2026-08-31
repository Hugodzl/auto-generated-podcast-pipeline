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

The fetch / preflight / generate / boost / file machinery lives in
`rabbit_hole.oneoff` and is under test in `tests/test_oneoff.py`; this file is
just the episode's data. Fix behaviour there, not here.
"""
import sys
from pathlib import Path

# This file lives in scripts/, but it imports the rabbit_hole package at the repo
# root. Running it as a plain script puts scripts/ (not root) on sys.path, so the
# import would fail under the Scheduled Task; add the root explicitly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rabbit_hole.oneoff import EpisodeSpec, run  # noqa: E402

PDF_DIR = ROOT / "data" / "pdfs" / "algorithms-episode"

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

SPEC = EpisodeSpec(
    slug="topic",
    label="topic",
    title=TITLE,
    topic_bucket=TOPIC_BUCKET,
    pdf_dir=PDF_DIR,
    pdf_sources=PDF_SOURCES,
    url_sources=URL_SOURCES,
    focus=FOCUS,
    language=LANGUAGE,
    audio_format=AUDIO_FORMAT,
    audio_length=AUDIO_LENGTH,
)


if __name__ == "__main__":
    sys.exit(run(SPEC, sys.argv[1:]))
