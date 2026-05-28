import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import keyring
from langdetect import LangDetectException, detect

from rabbit_hole.config import ApisConfig
from rabbit_hole.sources import FeedItem
from rabbit_hole.state import State, hash_str

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*]')
USER_AGENT = "RandomRabbitHole/1.0 (mailto:you@example.com)"

KEYRING_SERVICE = "rabbit_hole"
# Polite throttle, keyed by API (a cold run can fire >1000 lookups). Every
# provider gets a default safety-net interval so no endpoint can be hit in an
# unthrottled burst; Semantic Scholar's keyless tier (~1 req/sec shared) is the
# strictest. Patch these to 0 in tests.
_DEFAULT_MIN_INTERVAL = 0.25
_S2_MIN_INTERVAL = 1.1
_MAX_RETRIES = 3
_LAST_REQUEST_AT: dict[str, float] = {}


def _api_key(api: str) -> str | None:
    """Optional Semantic Scholar key from Credential Manager; None if unset."""
    if api != "semantic_scholar":
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, "semantic_scholar_api_key") or None
    except Exception:
        return None


def _min_interval(api: str, has_key: bool) -> float:
    if has_key:
        # A configured key relaxes that provider to the baseline, not to zero,
        # so the safety net still applies.
        return _DEFAULT_MIN_INTERVAL
    # Per-api overrides; every other provider gets the safety-net baseline.
    return {"semantic_scholar": _S2_MIN_INTERVAL}.get(api, _DEFAULT_MIN_INTERVAL)


def _throttle(api: str, has_key: bool) -> None:
    interval = _min_interval(api, has_key)
    if interval <= 0:
        return
    last = _LAST_REQUEST_AT.get(api)
    if last is not None:
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _LAST_REQUEST_AT[api] = time.monotonic()


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return 2.0 ** attempt


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

    api_key = _api_key(api)
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["x-api-key"] = api_key

    for attempt in range(_MAX_RETRIES + 1):
        _throttle(api, has_key=bool(api_key))
        try:
            resp = httpx.get(url, params=params, headers=headers,
                             timeout=10.0, follow_redirects=True)
        except httpx.HTTPError:
            return None
        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            time.sleep(_retry_delay(resp, attempt))
            continue
        try:
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        state.cache_set(api, key, data)
        return data
    return None


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
