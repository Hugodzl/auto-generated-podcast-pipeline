import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import keyring
import pytest
import respx

from rabbit_hole import resolver
from rabbit_hole.config import ApisConfig
from rabbit_hole.resolver import (
    extract_doi,
    extract_arxiv_id,
    arxiv_pdf_url,
    detect_language,
    Paper,
    biorxiv_pdf_url,
    hal_pdf_url,
    crossref_metadata,
)
from rabbit_hole.sources import FeedItem
from rabbit_hole.state import State


def make_item(link="https://www.nature.com/articles/s41586-024-12345",
              summary="", title="A paper"):
    return FeedItem(
        source_name="Nature", source_weight=1.0, title=title,
        summary=summary, link=link,
        published_at=datetime(2026, 5, 19, tzinfo=timezone.utc), raw={},
    )


@pytest.fixture(autouse=True)
def _fast_resolver(monkeypatch):
    """Keep the suite fast and isolated: no real sleeps, no real keyring reads,
    and reset the module-level S2 request-time map between tests."""
    resolver._LAST_REQUEST_AT.clear()
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(keyring, "get_password", lambda *_a, **_k: None)
    yield
    resolver._LAST_REQUEST_AT.clear()


def test_extract_doi_from_summary():
    item = make_item(summary="See doi:10.1038/s41586-024-12345 for details")
    assert extract_doi(item) == "10.1038/s41586-024-12345"


def test_extract_doi_from_url():
    item = make_item(link="https://doi.org/10.1126/science.abc1234")
    assert extract_doi(item) == "10.1126/science.abc1234"


def test_extract_doi_none_when_absent():
    item = make_item(link="https://example.com/news/story", summary="no identifiers")
    assert extract_doi(item) is None


def test_detect_language_french():
    assert detect_language("Une étude sur la communication des cétacés et leurs comportements sociaux") == "fr"


def test_detect_language_english_default():
    assert detect_language("A study of cetacean communication") == "en"


@respx.mock
def test_download_pdf_saves_file(tmp_path):
    from rabbit_hole.resolver import download_pdf
    respx.get("http://x/paper.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 fake")
    )
    dest = tmp_path / "out.pdf"
    download_pdf("http://x/paper.pdf", dest)
    assert dest.read_bytes().startswith(b"%PDF")


@respx.mock
def test_download_pdf_leaves_no_part_file(tmp_path):
    from rabbit_hole.resolver import download_pdf
    respx.get("http://x/p.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1.4 ok"))
    dest = tmp_path / "out.pdf"
    download_pdf("http://x/p.pdf", dest)
    assert dest.exists()
    assert not (tmp_path / "out.pdf.part").exists()


@respx.mock
def test_download_pdf_cleans_up_on_failure(tmp_path):
    import pytest
    from rabbit_hole.resolver import download_pdf
    respx.get("http://x/bad.pdf").mock(return_value=httpx.Response(500))
    dest = tmp_path / "bad.pdf"
    with pytest.raises(httpx.HTTPError):
        download_pdf("http://x/bad.pdf", dest)
    assert not dest.exists()
    assert not (tmp_path / "bad.pdf.part").exists()


def test_extract_doi_strips_trailing_paren_period():
    item = make_item(summary="(see doi:10.1038/s41586-024-12345.)", link="https://example.com/news")
    assert extract_doi(item) == "10.1038/s41586-024-12345"


@respx.mock
def test_unpaywall_returns_pdf_url(tmp_db, migrations_dir):
    from rabbit_hole.resolver import unpaywall_pdf_url
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get("https://api.unpaywall.org/v2/10.1038/x").mock(
        return_value=httpx.Response(200, json={
            "best_oa_location": {"url_for_pdf": "http://oa.org/paper.pdf"}
        })
    )
    url = unpaywall_pdf_url("10.1038/x", email="you@example.com", state=st)
    assert url == "http://oa.org/paper.pdf"


@respx.mock
def test_unpaywall_none_when_no_oa(tmp_db, migrations_dir):
    from rabbit_hole.resolver import unpaywall_pdf_url
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get("https://api.unpaywall.org/v2/10.1038/y").mock(
        return_value=httpx.Response(200, json={"best_oa_location": None})
    )
    assert unpaywall_pdf_url("10.1038/y", email="e@x.com", state=st) is None


@respx.mock
def test_semantic_scholar_pdf_url(tmp_db, migrations_dir):
    from rabbit_hole.resolver import semantic_scholar_pdf_url
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json={
            "data": [{"openAccessPdf": {"url": "http://s2.org/p.pdf"}}]
        })
    )
    url = semantic_scholar_pdf_url("Sperm whales have vowels", state=st)
    assert url == "http://s2.org/p.pdf"


@respx.mock
def test_resolve_via_unpaywall(tmp_db, migrations_dir, tmp_path):
    from rabbit_hole.resolver import resolve
    st = State(tmp_db, migrations_dir)
    st.init()
    item = make_item(
        link="https://www.nature.com/articles/x",
        summary="doi:10.1038/s41586-024-12345 cetacean communication study",
        title="Sperm whales have vowels in their codas",
    )
    respx.get("https://api.unpaywall.org/v2/10.1038/s41586-024-12345").mock(
        return_value=httpx.Response(200, json={
            "best_oa_location": {"url_for_pdf": "http://oa.org/p.pdf"},
            "title": "Sperm whales have vowels in their codas",
            "z_authors": [{"given": "A", "family": "Sharma"}],
        })
    )
    respx.get("http://oa.org/p.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 fake")
    )
    respx.get("https://api.crossref.org/works/10.1038/s41586-024-12345").mock(
        return_value=httpx.Response(200, json={
            "message": {
                "author": [{"given": "Pratyusha", "family": "Sharma"}],
                "title": ["Sperm whales have vowels in their codas"],
            }
        })
    )
    paper = resolve(item, ApisConfig(unpaywall_email="e@x.com"), st, pdf_dir=tmp_path)
    assert paper is not None
    assert paper.doi == "10.1038/s41586-024-12345"
    assert paper.pdf_path.exists()
    assert paper.language == "en"
    assert paper.authors == ["Pratyusha Sharma"]
    assert paper.abstract == item.summary


@respx.mock
def test_resolve_returns_none_when_no_pdf(tmp_db, migrations_dir, tmp_path):
    from rabbit_hole.resolver import resolve
    st = State(tmp_db, migrations_dir)
    st.init()
    item = make_item(link="https://example.com/news", summary="no doi here", title="News story")
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("https://api.archives-ouvertes.fr/search/").mock(
        return_value=httpx.Response(200, json={"response": {"numFound": 0, "docs": []}})
    )
    paper = resolve(item, ApisConfig(unpaywall_email="e@x.com"), st, pdf_dir=tmp_path)
    assert paper is None


@respx.mock
def test_resolve_sanitizes_windows_illegal_filename(tmp_db, migrations_dir, tmp_path):
    from rabbit_hole.resolver import resolve
    st = State(tmp_db, migrations_dir)
    st.init()
    # DOI containing a colon (legal in DOIs, illegal in Windows filenames)
    item = make_item(
        link="https://example.com/news",
        summary="doi:10.1038/s41586:2024:12345 study",
        title="Colon DOI paper",
    )
    respx.get("https://api.unpaywall.org/v2/10.1038/s41586:2024:12345").mock(
        return_value=httpx.Response(200, json={"best_oa_location": {"url_for_pdf": "http://oa.org/c.pdf"}})
    )
    respx.get("http://oa.org/c.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1.4 x"))
    respx.get("https://api.crossref.org/works/10.1038/s41586:2024:12345").mock(
        return_value=httpx.Response(200, json={"message": {}})
    )
    paper = resolve(item, ApisConfig(unpaywall_email="e@x.com"), st, pdf_dir=tmp_path)
    assert paper is not None
    assert paper.pdf_path.exists()
    # filename must contain no Windows-illegal characters
    assert not set(paper.pdf_path.name) & set('<>:"/\\|?*')


_S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"


@respx.mock
def test_get_json_retries_after_429_then_succeeds(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    route = respx.get(_S2_SEARCH).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"data": [{"openAccessPdf": {"url": "http://s2.org/p.pdf"}}]}),
        ]
    )
    url = resolver.semantic_scholar_pdf_url("Sperm whales have vowels", state=st)
    assert url == "http://s2.org/p.pdf"
    assert route.call_count == 2


@respx.mock
def test_get_json_returns_none_after_persistent_429(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    route = respx.get(_S2_SEARCH).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )
    result = resolver._get_json(_S2_SEARCH, "semantic_scholar", st, params={"query": "x"})
    assert result is None
    assert route.call_count >= 2  # retried, did not fail fast


@respx.mock
def test_throttle_can_be_disabled_with_zero_interval(tmp_db, migrations_dir, monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(resolver, "_S2_MIN_INTERVAL", 0)
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(_S2_SEARCH).mock(return_value=httpx.Response(200, json={"data": []}))
    resolver.semantic_scholar_pdf_url("query one", state=st)
    resolver.semantic_scholar_pdf_url("query two", state=st)
    assert sleeps == []


@respx.mock
def test_default_throttle_covers_non_s2_providers(tmp_db, migrations_dir, monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get("https://api.unpaywall.org/v2/10.1/a").mock(return_value=httpx.Response(200, json={}))
    respx.get("https://api.unpaywall.org/v2/10.1/b").mock(return_value=httpx.Response(200, json={}))
    resolver._get_json("https://api.unpaywall.org/v2/10.1/a", "unpaywall", st)
    resolver._get_json("https://api.unpaywall.org/v2/10.1/b", "unpaywall", st)
    assert any(s > 0 for s in sleeps)  # safety-net baseline spaces non-S2 providers too


@respx.mock
def test_api_key_keeps_s2_safety_net_throttle(tmp_db, migrations_dir, monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(keyring, "get_password", lambda service, key: "SECRET-KEY")
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(_S2_SEARCH).mock(return_value=httpx.Response(200, json={"data": []}))
    resolver.semantic_scholar_pdf_url("query one", state=st)
    resolver.semantic_scholar_pdf_url("query two", state=st)
    assert any(s > 0 for s in sleeps)  # a key relaxes S2 but does not fully disable the throttle


@respx.mock
def test_semantic_scholar_sends_api_key_header(tmp_db, migrations_dir, monkeypatch):
    monkeypatch.setattr(
        keyring, "get_password",
        lambda service, key: "SECRET-KEY" if key == "semantic_scholar_api_key" else None,
    )
    st = State(tmp_db, migrations_dir)
    st.init()
    route = respx.get(_S2_SEARCH).mock(
        return_value=httpx.Response(200, json={"data": [{"openAccessPdf": {"url": "http://s2.org/p.pdf"}}]})
    )
    url = resolver.semantic_scholar_pdf_url("Sperm whales have vowels", state=st)
    assert url == "http://s2.org/p.pdf"
    assert route.calls.last.request.headers.get("x-api-key") == "SECRET-KEY"


@respx.mock
def test_s2_requests_are_throttled(tmp_db, migrations_dir, monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(_S2_SEARCH).mock(return_value=httpx.Response(200, json={"data": []}))
    resolver.semantic_scholar_pdf_url("query one", state=st)
    resolver.semantic_scholar_pdf_url("query two", state=st)
    assert any(s > 0 for s in sleeps)  # second uncached S2 call was spaced out


# ---------------------------------------------------------------------------
# arXiv: extract_arxiv_id
# ---------------------------------------------------------------------------

def test_extract_arxiv_id_from_abs_link():
    item = make_item(link="http://arxiv.org/abs/2507.14805")
    assert extract_arxiv_id(item) == "2507.14805"


def test_extract_arxiv_id_from_pdf_link_with_version():
    item = make_item(link="https://arxiv.org/pdf/2507.14805v2")
    assert extract_arxiv_id(item) == "2507.14805v2"


def test_extract_arxiv_id_from_summary_prefix():
    item = make_item(summary="arXiv:2507.14805 — a study of language models")
    assert extract_arxiv_id(item) == "2507.14805"


def test_extract_arxiv_id_returns_none_for_non_arxiv_item():
    # default make_item has a Nature link — must NOT false-match
    item = make_item()
    assert extract_arxiv_id(item) is None


def test_extract_arxiv_id_case_insensitive_prefix():
    item = make_item(summary="ARXIV: 2507.14805 some title")
    assert extract_arxiv_id(item) == "2507.14805"


# ---------------------------------------------------------------------------
# arXiv: arxiv_pdf_url
# ---------------------------------------------------------------------------

def test_arxiv_pdf_url_builds_url():
    item = make_item(link="http://arxiv.org/abs/2507.14805")
    assert arxiv_pdf_url(item) == "https://arxiv.org/pdf/2507.14805"


def test_arxiv_pdf_url_returns_none_for_non_arxiv_item():
    item = make_item()  # Nature link, no arXiv ID
    assert arxiv_pdf_url(item) is None


def test_extract_arxiv_id_from_html_link():
    item = make_item(link="https://arxiv.org/html/2507.14805")
    assert extract_arxiv_id(item) == "2507.14805"


def test_arxiv_pdf_url_preserves_version():
    item = make_item(link="http://arxiv.org/abs/2507.14805v3")
    assert arxiv_pdf_url(item) == "https://arxiv.org/pdf/2507.14805v3"


# ---------------------------------------------------------------------------
# Integration: resolve() picks arXiv before S2 when DOI is absent
# ---------------------------------------------------------------------------

@respx.mock
def test_resolve_via_arxiv(tmp_db, migrations_dir, tmp_path):
    from rabbit_hole.resolver import resolve
    st = State(tmp_db, migrations_dir)
    st.init()
    item = make_item(
        link="http://arxiv.org/abs/2507.14805",
        summary="A study of language model scaling",
        title="Scaling laws revisited",
    )
    # arXiv PDF URL is deterministic — no Unpaywall call (no DOI), no S2 call
    respx.get("https://arxiv.org/pdf/2507.14805").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 fake")
    )
    paper = resolve(item, ApisConfig(unpaywall_email="e@x.com"), st, pdf_dir=tmp_path)
    assert paper is not None
    assert paper.pdf_path.exists()
    assert paper.doi is None


# ---------------------------------------------------------------------------
# bioRxiv / medRxiv: biorxiv_pdf_url
# ---------------------------------------------------------------------------

_BIORXIV_DOI = "10.1101/2024.01.01.123456"
_BIORXIV_API_BASE = "https://api.biorxiv.org/details"


@respx.mock
def test_biorxiv_returns_latest_version(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(f"{_BIORXIV_API_BASE}/biorxiv/{_BIORXIV_DOI}").mock(
        return_value=httpx.Response(200, json={
            "messages": [{"status": "ok"}],
            "collection": [
                {"doi": _BIORXIV_DOI, "version": "1"},
                {"doi": _BIORXIV_DOI, "version": "2"},
            ],
        })
    )
    url = biorxiv_pdf_url(_BIORXIV_DOI, st)
    assert url == f"https://www.biorxiv.org/content/{_BIORXIV_DOI}v2.full.pdf"


@respx.mock
def test_biorxiv_falls_back_to_medrxiv(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    # biorxiv returns empty collection
    respx.get(f"{_BIORXIV_API_BASE}/biorxiv/{_BIORXIV_DOI}").mock(
        return_value=httpx.Response(200, json={
            "messages": [{"status": "no posts found"}],
            "collection": [],
        })
    )
    # medrxiv has the record
    respx.get(f"{_BIORXIV_API_BASE}/medrxiv/{_BIORXIV_DOI}").mock(
        return_value=httpx.Response(200, json={
            "messages": [{"status": "ok"}],
            "collection": [
                {"doi": _BIORXIV_DOI, "version": "3"},
            ],
        })
    )
    url = biorxiv_pdf_url(_BIORXIV_DOI, st)
    assert url == f"https://www.medrxiv.org/content/{_BIORXIV_DOI}v3.full.pdf"


@respx.mock
def test_biorxiv_picks_max_version_not_last(tmp_db, migrations_dir):
    """Out-of-order versions — max() must win, not the last element."""
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(f"{_BIORXIV_API_BASE}/biorxiv/{_BIORXIV_DOI}").mock(
        return_value=httpx.Response(200, json={
            "messages": [{"status": "ok"}],
            "collection": [
                {"doi": _BIORXIV_DOI, "version": "3"},
                {"doi": _BIORXIV_DOI, "version": "1"},
                {"doi": _BIORXIV_DOI, "version": "2"},
            ],
        })
    )
    url = biorxiv_pdf_url(_BIORXIV_DOI, st)
    assert url == f"https://www.biorxiv.org/content/{_BIORXIV_DOI}v3.full.pdf"


@respx.mock
def test_biorxiv_returns_none_when_not_found(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(f"{_BIORXIV_API_BASE}/biorxiv/{_BIORXIV_DOI}").mock(
        return_value=httpx.Response(200, json={
            "messages": [{"status": "no posts found"}],
            "collection": [],
        })
    )
    respx.get(f"{_BIORXIV_API_BASE}/medrxiv/{_BIORXIV_DOI}").mock(
        return_value=httpx.Response(200, json={
            "messages": [{"status": "no posts found"}],
            "collection": [],
        })
    )
    assert biorxiv_pdf_url(_BIORXIV_DOI, st) is None


@respx.mock
def test_biorxiv_skips_server_with_unparseable_version(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    # biorxiv has the record but with a non-integer version -> the loop must
    # skip to medrxiv (not abandon the fallback) and ultimately return None.
    respx.get(f"{_BIORXIV_API_BASE}/biorxiv/{_BIORXIV_DOI}").mock(
        return_value=httpx.Response(200, json={
            "messages": [{"status": "ok"}],
            "collection": [{"doi": _BIORXIV_DOI, "version": "abc"}],
        })
    )
    respx.get(f"{_BIORXIV_API_BASE}/medrxiv/{_BIORXIV_DOI}").mock(
        return_value=httpx.Response(200, json={
            "messages": [{"status": "no posts found"}],
            "collection": [],
        })
    )
    assert biorxiv_pdf_url(_BIORXIV_DOI, st) is None


# ---------------------------------------------------------------------------
# HAL: hal_pdf_url
# ---------------------------------------------------------------------------

_HAL_SEARCH = "https://api.archives-ouvertes.fr/search/"


@respx.mock
def test_hal_returns_file_url(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(_HAL_SEARCH).mock(
        return_value=httpx.Response(200, json={
            "response": {
                "numFound": 1,
                "docs": [{"fileMain_s": "https://hal.science/hal-04123456/document"}],
            }
        })
    )
    url = hal_pdf_url("Some paper title", st)
    assert url == "https://hal.science/hal-04123456/document"


@respx.mock
def test_hal_returns_none_when_no_docs(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(_HAL_SEARCH).mock(
        return_value=httpx.Response(200, json={
            "response": {"numFound": 0, "docs": []}
        })
    )
    assert hal_pdf_url("Nonexistent paper", st) is None


@respx.mock
def test_hal_returns_none_when_no_file_main(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(_HAL_SEARCH).mock(
        return_value=httpx.Response(200, json={
            "response": {
                "numFound": 1,
                "docs": [{"title_s": ["Paper without full text"]}],
            }
        })
    )
    assert hal_pdf_url("Paper without full text", st) is None


@respx.mock
def test_hal_escapes_quotes_in_title(tmp_db, migrations_dir):
    """A title with embedded double-quotes must be escaped in the Solr query."""
    st = State(tmp_db, migrations_dir)
    st.init()
    route = respx.get(_HAL_SEARCH).mock(
        return_value=httpx.Response(200, json={
            "response": {
                "numFound": 1,
                "docs": [{"fileMain_s": "https://hal.science/x/document"}],
            }
        })
    )
    url = hal_pdf_url('A "quoted" title', st)
    assert url == "https://hal.science/x/document"
    q = route.calls.last.request.url.params["q"]
    assert '\\"' in q  # the embedded quote was backslash-escaped


# ---------------------------------------------------------------------------
# Crossref: crossref_metadata
# ---------------------------------------------------------------------------

_CROSSREF_BASE = "https://api.crossref.org/works"
_CROSSREF_DOI = "10.1038/s41586-024-12345"


@respx.mock
def test_crossref_metadata_parses_authors_and_strips_jats(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(f"{_CROSSREF_BASE}/{_CROSSREF_DOI}").mock(
        return_value=httpx.Response(200, json={
            "message": {
                "author": [
                    {"given": "Ada", "family": "Lovelace"},
                    {"given": "Alan", "family": "Turing"},
                ],
                "abstract": "<jats:p>A study of things.</jats:p>",
                "title": ["Some title"],
            }
        })
    )
    result = crossref_metadata(_CROSSREF_DOI, "e@x.com", st)
    assert result is not None
    assert result["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert result["abstract"] == "A study of things."


@respx.mock
def test_crossref_metadata_handles_org_author(tmp_db, migrations_dir):
    """An org entry uses the 'name' field rather than given/family."""
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(f"{_CROSSREF_BASE}/{_CROSSREF_DOI}").mock(
        return_value=httpx.Response(200, json={
            "message": {
                "author": [
                    {"given": "Ada", "family": "Lovelace"},
                    {"name": "The Consortium"},
                ],
                "title": ["Some title"],
            }
        })
    )
    result = crossref_metadata(_CROSSREF_DOI, "e@x.com", st)
    assert result is not None
    assert result["authors"] == ["Ada Lovelace", "The Consortium"]
    assert result["abstract"] is None


@respx.mock
def test_crossref_metadata_returns_none_on_404(tmp_db, migrations_dir):
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(f"{_CROSSREF_BASE}/{_CROSSREF_DOI}").mock(
        return_value=httpx.Response(404)
    )
    result = crossref_metadata(_CROSSREF_DOI, "e@x.com", st)
    assert result is None


@respx.mock
def test_crossref_metadata_decodes_html_entities(tmp_db, migrations_dir):
    """HTML/XML entities in the abstract are decoded after tag stripping."""
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(f"{_CROSSREF_BASE}/{_CROSSREF_DOI}").mock(
        return_value=httpx.Response(200, json={
            "message": {
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "abstract": "<jats:p>Foxes &amp; hounds &#x2013; a study.</jats:p>",
            }
        })
    )
    result = crossref_metadata(_CROSSREF_DOI, "e@x.com", st)
    assert result is not None
    assert result["abstract"] == "Foxes & hounds – a study."


@respx.mock
def test_crossref_metadata_no_author_key(tmp_db, migrations_dir):
    """A Crossref message without an 'author' key returns an empty authors list."""
    st = State(tmp_db, migrations_dir)
    st.init()
    respx.get(f"{_CROSSREF_BASE}/{_CROSSREF_DOI}").mock(
        return_value=httpx.Response(200, json={
            "message": {"abstract": "<jats:p>Text.</jats:p>"}
        })
    )
    result = crossref_metadata(_CROSSREF_DOI, "e@x.com", st)
    assert result is not None
    assert result["authors"] == []
    assert result["abstract"] == "Text."


# ---------------------------------------------------------------------------
# Crossref integration: resolve() enrichment
# ---------------------------------------------------------------------------

@respx.mock
def test_resolve_enriches_authors_via_crossref(tmp_db, migrations_dir, tmp_path):
    """A DOI-bearing item that resolves via Unpaywall gets authors from Crossref."""
    from rabbit_hole.resolver import resolve
    st = State(tmp_db, migrations_dir)
    st.init()
    item = make_item(
        link="https://www.nature.com/articles/x",
        summary="doi:10.1038/s41586-024-12345 a study",
        title="A whale paper",
    )
    respx.get("https://api.unpaywall.org/v2/10.1038/s41586-024-12345").mock(
        return_value=httpx.Response(200, json={
            "best_oa_location": {"url_for_pdf": "http://oa.org/w.pdf"}
        })
    )
    respx.get("http://oa.org/w.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 fake")
    )
    respx.get(f"{_CROSSREF_BASE}/{_CROSSREF_DOI}").mock(
        return_value=httpx.Response(200, json={
            "message": {
                "author": [
                    {"given": "Ada", "family": "Lovelace"},
                    {"given": "Alan", "family": "Turing"},
                ],
                "title": ["A whale paper"],
            }
        })
    )
    paper = resolve(item, ApisConfig(unpaywall_email="e@x.com"), st, pdf_dir=tmp_path)
    assert paper is not None
    assert paper.authors == ["Ada Lovelace", "Alan Turing"]


@respx.mock
def test_resolve_fills_abstract_when_summary_empty(tmp_db, migrations_dir, tmp_path):
    """When item.summary is empty, Crossref abstract is used."""
    from rabbit_hole.resolver import resolve
    st = State(tmp_db, migrations_dir)
    st.init()
    item = FeedItem(
        source_name="Nature", source_weight=1.0, title="A whale paper",
        summary="",
        link="https://doi.org/10.1038/s41586-024-12345",
        published_at=datetime(2026, 5, 19, tzinfo=timezone.utc), raw={},
    )
    respx.get("https://api.unpaywall.org/v2/10.1038/s41586-024-12345").mock(
        return_value=httpx.Response(200, json={
            "best_oa_location": {"url_for_pdf": "http://oa.org/w2.pdf"}
        })
    )
    respx.get("http://oa.org/w2.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 fake")
    )
    respx.get(f"{_CROSSREF_BASE}/{_CROSSREF_DOI}").mock(
        return_value=httpx.Response(200, json={
            "message": {
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "abstract": "<jats:p>A study of things.</jats:p>",
                "title": ["A whale paper"],
            }
        })
    )
    paper = resolve(item, ApisConfig(unpaywall_email="e@x.com"), st, pdf_dir=tmp_path)
    assert paper is not None
    assert paper.abstract == "A study of things."


@respx.mock
def test_resolve_conservative_abstract_keeps_existing_summary(tmp_db, migrations_dir, tmp_path):
    """When item has a non-empty summary, Crossref abstract is NOT used."""
    from rabbit_hole.resolver import resolve
    st = State(tmp_db, migrations_dir)
    st.init()
    item = FeedItem(
        source_name="Nature", source_weight=1.0, title="A whale paper",
        summary="Original summary from the feed.",
        link="https://doi.org/10.1038/s41586-024-12345",
        published_at=datetime(2026, 5, 19, tzinfo=timezone.utc), raw={},
    )
    respx.get("https://api.unpaywall.org/v2/10.1038/s41586-024-12345").mock(
        return_value=httpx.Response(200, json={
            "best_oa_location": {"url_for_pdf": "http://oa.org/w3.pdf"}
        })
    )
    respx.get("http://oa.org/w3.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 fake")
    )
    respx.get(f"{_CROSSREF_BASE}/{_CROSSREF_DOI}").mock(
        return_value=httpx.Response(200, json={
            "message": {
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "abstract": "<jats:p>Different Crossref abstract.</jats:p>",
                "title": ["A whale paper"],
            }
        })
    )
    paper = resolve(item, ApisConfig(unpaywall_email="e@x.com"), st, pdf_dir=tmp_path)
    assert paper is not None
    assert paper.abstract == "Original summary from the feed."


@respx.mock
def test_resolve_with_doi_but_no_pdf_returns_none(tmp_db, migrations_dir, tmp_path):
    """Crossref enrichment cannot bypass the no-PDF skip gate."""
    from rabbit_hole.resolver import resolve
    st = State(tmp_db, migrations_dir)
    st.init()
    # Item has a DOI but no arXiv ID, so arXiv is also skipped.
    item = FeedItem(
        source_name="Nature", source_weight=1.0, title="A whale paper",
        summary="",
        link="https://doi.org/10.1038/s41586-024-12345",
        published_at=datetime(2026, 5, 19, tzinfo=timezone.utc), raw={},
    )
    # Unpaywall: no OA location
    respx.get("https://api.unpaywall.org/v2/10.1038/s41586-024-12345").mock(
        return_value=httpx.Response(200, json={"best_oa_location": None})
    )
    # bioRxiv: not found
    respx.get("https://api.biorxiv.org/details/biorxiv/10.1038/s41586-024-12345").mock(
        return_value=httpx.Response(200, json={"collection": []})
    )
    respx.get("https://api.biorxiv.org/details/medrxiv/10.1038/s41586-024-12345").mock(
        return_value=httpx.Response(200, json={"collection": []})
    )
    # S2: no results
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    # HAL: no results
    respx.get(_HAL_SEARCH).mock(
        return_value=httpx.Response(200, json={"response": {"docs": []}})
    )
    # Crossref must NOT be called (no PDF, so we never reach enrichment)
    # — the gate must fire first and return None.
    result = resolve(item, ApisConfig(unpaywall_email="e@x.com"), st, pdf_dir=tmp_path)
    assert result is None


def test_detect_language_falls_back_to_en_on_undetectable_text():
    # Symbol-only text raises LangDetectException internally -> "en" fallback,
    # which downstream becomes NLM's --language en.
    assert detect_language("42 :: ???") == "en"


def test_paper_from_link_builds_a_paper_with_no_pdf():
    """A URL-sourced item skips the whole PDF cascade: NotebookLM reads the page."""
    item = make_item(link="https://aeon.co/essays/x", title="An essay",
                     summary="On knowing and not knowing.")
    paper = resolver.paper_from_link(item)
    assert paper.pdf_path is None
    assert paper.title == "An essay"
    assert paper.abstract == "On knowing and not knowing."
    assert paper.language == "en"
    assert paper.source is item
