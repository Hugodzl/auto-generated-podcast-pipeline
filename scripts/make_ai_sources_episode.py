"""One-off: build a themed episode about where AI answers actually come from -
which parts of the web AI systems are allowed to read, how they attach citations
to what they say, and what that pipeline does to the credibility of the answer.

Built 2026-08-11 for "AI doesn't read the internet - it reads what let it in".
Same shape as make_topic_episode.py (the recommender-systems episode): it bypasses
the RSS -> resolve -> rank front of the pipeline and reuses the back end unchanged:
generate(sources=[...]) -> boost_loudness(+40%) -> file_episode. No RSS state is
touched, so this can't collide with the weekly run's dedup or recovery.

Modes:
  python scripts/make_ai_sources_episode.py --fetch --check   # download sources, preflight
  python scripts/make_ai_sources_episode.py --check           # preflight only, no NLM time spent
  python scripts/make_ai_sources_episode.py --check --no-probe # skip the ~1 min nlm URL probe
  python scripts/make_ai_sources_episode.py                   # full generate + boost + file

The preflight asks nlm itself whether it can ingest each URL source, because HTTP
reachability turned out to be the wrong oracle in both directions - see
_urls_nlm_can_ingest. The first run of this episode died four minutes in on a URL
that answered 200 to httpx and that nlm then refused.

The real run is meant to be launched DETACHED (scripts/run_ai_sources_episode_detached.ps1)
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

PDF_DIR = ROOT / "data" / "pdfs" / "ai-sources-episode"

# Episode identity. file_episode() names the file "{topic} - {title}{ext}" and
# tag_audio() embeds the title; the Plex `date` tag is the generation day.
TITLE = "AI doesn't read the internet — it reads what let it in"
TOPIC_BUCKET = "Technology and society"
LANGUAGE = "en"
AUDIO_FORMAT = "deep_dive"
AUDIO_LENGTH = "long"

# Peer-reviewed and preprint papers, downloaded locally so the run doesn't depend
# on a publisher being reachable (or bot-blocking) at generate time. The origin URL
# is kept beside each filename because data/ is gitignored: without it the reading
# list would not be reproducible from the repo. `--fetch` re-downloads what's missing.
#
# Numbering follows the episode's five movements: 01-02 the gate (who lets crawlers
# in), 03-06 how citations get attached after the fact, 07-10 the synthetic feedback
# loop, 11-13 writing for the machine, 14-17 the shopping case.
PDF_SOURCES = [
    # --- 1. The gate: which web AI is allowed to read ---
    # Steinacker-Olsztyn et al., WWW 2026. THE anchor: 60.0% of reputable news sites
    # disallow >=1 AI crawler vs 9.1% of misinformation sites (15.5 vs <1 user agents).
    ("01-is-misinformation-more-open-robots-txt-gatekeeping.pdf",
     "https://arxiv.org/pdf/2510.10315"),
    # Longpre et al. (Data Provenance Initiative), 2024 - the longitudinal audit:
    # ~28% of the most critical C4 tokens fully restricted by robots.txt, 45% by ToS.
    ("02-consent-in-crisis-decline-of-the-ai-data-commons.pdf",
     "https://arxiv.org/pdf/2407.14933"),

    # --- 2. Post hoc citation: the answer first, the source afterwards ---
    # Wallat et al., ICTIR 2025 - names the mechanism (post-rationalization) and
    # measures it: up to 57% of citations are correct but not faithful.
    ("03-correctness-is-not-faithfulness-in-rag-attributions.pdf",
     "https://arxiv.org/pdf/2412.18004"),
    # The G-Cite vs P-Cite taxonomy - cite while generating vs bolt citations on after.
    ("04-generation-time-vs-post-hoc-citation.pdf",
     "https://arxiv.org/pdf/2509.21557"),
    # Liu, Zhang & Liang 2023 - the original verifiability audit of generative search:
    # fluent, confident answers whose sentences are often not supported by the citation.
    ("05-evaluating-verifiability-in-generative-search-engines.pdf",
     "https://arxiv.org/pdf/2304.09848"),
    # The same question asked of 2026-era deep-research agents, which cite far more.
    ("06-cited-but-not-verified-deep-research-agents.pdf",
     "https://arxiv.org/pdf/2605.06635"),

    # --- 3. The synthetic loop: AI citing AI ---
    # Allaham & Diakopoulos, 2026 - ~16% (1 in 6) of sources cited by ChatGPT, Copilot,
    # Gemini and Perplexity across 712 real queries show signs of being AI-generated.
    ("07-synthetic-sources-auditing-generative-search-citations.pdf",
     "https://arxiv.org/pdf/2605.23684"),
    # Dai et al., KDD 2024 - why that happens structurally: retrievers systematically
    # rank LLM-written text above human text ("source bias").
    ("08-neural-retrievers-are-biased-toward-llm-generated-content.pdf",
     "https://arxiv.org/pdf/2310.20501"),
    # The causal mechanism underneath source bias: retrievers overrate low-perplexity
    # (fluent, predictable) text, which is exactly what an LLM produces.
    ("09-perplexity-trap-retrievers-overrate-low-perplexity-documents.pdf",
     "https://arxiv.org/pdf/2503.08684"),
    # Chen et al., ACL 2024 - simulate the loop over many rounds: human content gets
    # progressively buried, and early errors calcify. The long-run version of 1-in-6.
    ("10-spiral-of-silence-llm-killing-information-retrieval.pdf",
     "https://arxiv.org/pdf/2404.10496"),

    # --- 4. Writing for the machine ---
    # Aggarwal et al., KDD 2024 - the counter-evidence to "optimizing for AI isn't a
    # strategy": black-box tweaks lift a source's visibility in AI answers up to 40%.
    ("11-geo-generative-engine-optimization.pdf",
     "https://arxiv.org/pdf/2311.09735"),
    # The adversarial end of the same idea: a strategic text sequence planted on a
    # product page reliably promotes it into the model's top recommendation.
    ("12-manipulating-llms-to-increase-product-visibility.pdf",
     "https://arxiv.org/pdf/2404.07981"),
    # And against a live conversational search engine: injected page content moves the
    # ranking, which is the failure mode a "best of" page could exploit on purpose.
    ("13-ranking-manipulation-for-conversational-search-engines.pdf",
     "https://arxiv.org/pdf/2406.03589"),

    # --- 5. The shopping case: "best of" lists as evidence ---
    # Allouah et al., WWW 2026 - what agents actually buy: strong position bias, a
    # penalty on "sponsored" tags, a reward for platform endorsements, and sellers
    # winning share back with query-conditional description tweaks.
    ("14-what-is-your-ai-agent-buying-agentic-e-commerce.pdf",
     "https://arxiv.org/pdf/2508.02630"),
    # Cognitive biases (social proof, framing) move LLM product rankings measurably.
    ("15-bias-beware-cognitive-biases-in-llm-product-recommendations.pdf",
     "https://arxiv.org/pdf/2502.01349"),
    # Incumbent advantage: LLMs over-recommend brands they already know about.
    ("16-incumbent-advantage-brand-bias-in-llm-recommendation.pdf",
     "https://arxiv.org/pdf/2606.17443"),
    # Whether any of this reaches a purchase - AI brand recommendations on the open web.
    ("17-from-prompt-to-purchase-ai-brand-recommendations.pdf",
     "https://arxiv.org/pdf/2606.10907"),
]

# Reporting and industry measurement with no stable open PDF. These carry the numbers
# the papers don't: the newsroom-level blocking rate, how much of the new web is
# machine-written, and the deliberate campaign to get propaganda into AI answers.
URL_SOURCES = [
    # NewsGuard - 67% of top news sites block AI chatbots, and the consequence:
    # chatbots fall back on lower-quality sources. The bridge from claim 1 to claim 6.
    "https://www.newsguardtech.com/special-reports/"
    "67-percent-of-top-news-sites-block-ai-chatbots/",
    # The Tow Center / CJR study - eight AI search tools, 1,600 queries: wrong
    # attributions, fabricated URLs, and unwavering confidence while doing it.
    # cjr.org 403s a plain httpx client but NotebookLM ingests it fine; the reverse
    # was also true of a substitute that httpx fetched happily and nlm refused.
    # See _urls_nlm_can_ingest below - nlm is the only oracle worth asking.
    "https://www.cjr.org/tow_center/"
    "we-compared-eight-ai-search-engines-theyre-all-bad-at-citing-news.php",
    # Graphite - measurement of how much new web writing is machine-written (~52% of
    # a 65k-article sample), and the caveat that publishing share != visibility share.
    "https://graphite.io/five-percent/more-articles-are-now-created-by-ai-than-humans",
    # Cloudflare - AI crawler traffic by purpose, and crawl-to-referral ratios: the
    # economic reason publishers started closing the door in the first place.
    "https://blog.cloudflare.com/ai-crawler-traffic-by-purpose-and-industry/",
    # Bulletin of the Atomic Scientists on the Pravda network and "LLM grooming":
    # flooding the crawlable web specifically so chatbots repeat it.
    "https://thebulletin.org/2025/03/"
    "russian-networks-flood-the-internet-with-propaganda-aiming-to-corrupt-ai-chatbots/",
]

FOCUS = (
    "A long, comprehensive deep dive on where an AI answer's sources actually come "
    "from, and what that pipeline does to how much the answer is worth. The organizing "
    "claim: an AI system does not read the internet. It reads the subset of the "
    "internet that left the door open, then attaches citations to an answer it has "
    "largely already formed. Treat this as claim-checking, not cheerleading: several "
    "specific popular claims are on the table, and the job is to say which the "
    "evidence supports, which are overstated, and which are simply wrong. Always give "
    "the real number and who measured it, and say when a number comes from a single "
    "study, a preprint, an industry vendor, or an AI-detection tool that can itself be "
    "wrong. Move through five movements.\n\n"
    "(1) THE GATE. Test the claim that roughly 60% of reputable sites disallow at "
    "least one AI crawler. It holds up in the WWW 2026 robots.txt study - but the "
    "finding that matters is the asymmetry: about 60% of reputable news sites block at "
    "least one AI crawler, versus about 9% of misinformation sites; reputable sites "
    "block around 15 AI user agents each, misinformation sites fewer than one. Add the "
    "longitudinal picture from the Consent in Crisis audit and the newsroom-level rate "
    "from NewsGuard. Then test the related claim that AI can only read part of the "
    "internet. Explain what robots.txt actually is - a request, not a wall, with no "
    "enforcement - and why it is honored unevenly. Cover the economics: crawl-to-referral "
    "ratios, and the fact that blocking measurably costs publishers human traffic, so "
    "this is a real dilemma rather than an obvious choice.\n\n"
    "(2) POST HOC CITATION. Test the claim that AI deliberately hunts for sources that "
    "confirm the answer it already has. Be precise about intent: nothing here shows "
    "intent, and the mechanism is more interesting than intent would be. Distinguish "
    "citing while generating from bolting citations on afterwards, and name the measured "
    "failure - post-rationalization, where a citation is correct in the sense that the "
    "document does support the claim, yet the model did not actually rely on it. Give "
    "the numbers from the faithfulness work and the earlier verifiability audits, plus "
    "the Tow Center finding on wrong attributions, fabricated URLs, and confident "
    "delivery. Explain why the user-facing effect resembles motivated reasoning even "
    "with no motive: retrieval is steered by a query the model wrote, and a citation "
    "that looks like support is rewarded whether or not it did any work.\n\n"
    "(3) THE SYNTHETIC LOOP. Test the claim that about 1 in 6 sources AI uses is itself "
    "AI-generated. State it carefully - roughly 16% of successfully-scraped cited "
    "sources across four generative search engines and 712 real queries showed signs of "
    "AI generation, and detection is probabilistic, so treat it as an estimate with real "
    "error bars. Then explain why it is not a fluke: retrievers exhibit source bias, "
    "ranking machine-written text above human-written text, and the reason is almost "
    "funny - retrieval models overrate low-perplexity text, meaning smooth, predictable, "
    "unsurprising prose, which is exactly what a language model produces. Machine text "
    "is not selected for being right; it is selected for reading like an answer. Add the "
    "simulations of the loop running for many rounds, where human content gets "
    "progressively buried and early errors calcify. Set that beside the measurement "
    "that around half of newly published web articles are machine-written, and the "
    "caveat that publishing share is not visibility share.\n\n"
    "(4) WRITING FOR THE MACHINE. Push back on the claim that building pages for AI to "
    "pick up is not a real strategy. The evidence says it is: black-box optimization of "
    "a page lifts its visibility in AI answers by as much as 40%, more for smaller sites "
    "than for incumbents, and adversarial work shows how the appearance of authority is "
    "manufactured: a strategic text sequence planted on a page can promote it into the "
    "model's top recommendation, and injected page content can move rankings inside a "
    "live conversational search engine. The uncomfortable version: the "
    "actors best set up to exploit this are the ones with no reason to block a crawler "
    "and no reputation to protect - hence the propaganda network that publishes millions "
    "of articles nobody reads, aimed at the crawler rather than the reader. Connect this "
    "back to movement 1: the sites that said no are exactly the ones you would want in "
    "the answer.\n\n"
    "(5) THE SHOPPING CASE. Work the concrete example: ask an AI agent for the best "
    "product and it leans on 'best of' roundups, including ones published by a brand "
    "that put itself at the top, without weighing the conflict of interest a human "
    "reader would notice. Be honest that the direct study of that specific self-ranking "
    "failure is thin; use what has actually been measured on agentic shopping - strong "
    "position bias, a penalty on explicit 'sponsored' labels alongside a reward for "
    "platform endorsements that are also commercially placed, incumbent brand bias, "
    "cognitive-bias levers like social proof, and sellers who regain share by rewriting "
    "descriptions for the agent. The through-line: the agent reads the surface signals "
    "of credibility and misses the incentive behind them.\n\n"
    "Close on what actually follows for a listener, without a lecture: which questions "
    "this pipeline handles well, which it handles badly, and why 'it gave me sources' is "
    "weaker evidence than it looks. Sit with the disagreements between the sources "
    "rather than resolving them. Thoughtful, curious, skeptical of both hype and "
    "dismissal - and skeptical of the tidy version of these claims too."
)

SPEC = EpisodeSpec(
    slug="ai_sources",
    label="AI-sources",
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
