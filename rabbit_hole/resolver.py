import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from langdetect import LangDetectException, detect

from rabbit_hole.config import ApisConfig
from rabbit_hole.sources import FeedItem
from rabbit_hole.state import State, hash_str

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*]')
USER_AGENT = "RandomRabbitHole/1.0 (mailto:you@example.com)"


@dataclass
class Paper:
    doi: str | None
    title: str
    authors: list[str]
    abstract: str
    published_at: datetime
    pdf_path: Path
    language: str
    source: FeedItem


def extract_doi(item: FeedItem) -> str | None:
    for text in (item.link, item.summary, item.title):
        m = DOI_RE.search(text or "")
        if m:
            return m.group(0).rstrip(".,;:)")
    return None


def detect_language(text: str) -> str:
    try:
        lang = detect(text)
    except LangDetectException:
        return "en"
    return "fr" if lang == "fr" else "en"


def _get_json(url: str, api: str, state: State, params: dict | None = None) -> dict | None:
    key = hash_str(url + str(sorted((params or {}).items())))
    cached = state.cache_get(api, key, ttl_days=30)
    if cached is not None:
        return cached
    try:
        resp = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT},
                         timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    state.cache_set(api, key, data)
    return data


def download_pdf(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT},
                          timeout=30.0, follow_redirects=True) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def unpaywall_pdf_url(doi: str, email: str, state: State) -> str | None:
    data = _get_json(f"https://api.unpaywall.org/v2/{doi}", "unpaywall", state,
                     params={"email": email})
    if not data:
        return None
    loc = data.get("best_oa_location")
    if loc and loc.get("url_for_pdf"):
        return loc["url_for_pdf"]
    return None


def semantic_scholar_pdf_url(title: str, state: State) -> str | None:
    data = _get_json(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        "semantic_scholar", state,
        params={"query": title, "fields": "openAccessPdf", "limit": 1},
    )
    if not data or not data.get("data"):
        return None
    oa = data["data"][0].get("openAccessPdf")
    return oa.get("url") if oa else None


def _candidate_pdf_urls(item: FeedItem, doi: str | None, apis: ApisConfig,
                        state: State):
    """Yield candidate PDF URLs in cascade priority order."""
    if doi:
        url = unpaywall_pdf_url(doi, apis.unpaywall_email, state)
        if url:
            yield url
    url = semantic_scholar_pdf_url(item.title, state)
    if url:
        yield url


def resolve(item: FeedItem, apis: ApisConfig, state: State, pdf_dir: Path) -> Paper | None:
    doi = extract_doi(item)
    pdf_path = None
    for url in _candidate_pdf_urls(item, doi, apis, state):
        name = _ILLEGAL_FS.sub("_", doi or hash_str(item.link)) + ".pdf"
        dest = Path(pdf_dir) / name
        try:
            if not dest.exists():
                download_pdf(url, dest)
            pdf_path = dest
            break
        except (httpx.HTTPError, OSError):
            continue
    if pdf_path is None:
        return None

    text_for_lang = f"{item.title} {item.summary}".strip()
    return Paper(
        doi=doi,
        title=item.title,
        authors=[],
        abstract=item.summary,
        published_at=item.published_at,
        pdf_path=pdf_path,
        language=detect_language(text_for_lang),
        source=item,
    )
