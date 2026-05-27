import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import keyring
import pytest
import respx

from rabbit_hole import resolver
from rabbit_hole.config import ApisConfig
from rabbit_hole.resolver import extract_doi, detect_language, Paper
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
    paper = resolve(item, ApisConfig(unpaywall_email="e@x.com"), st, pdf_dir=tmp_path)
    assert paper is not None
    assert paper.doi == "10.1038/s41586-024-12345"
    assert paper.pdf_path.exists()
    assert paper.language == "en"


@respx.mock
def test_resolve_returns_none_when_no_pdf(tmp_db, migrations_dir, tmp_path):
    from rabbit_hole.resolver import resolve
    st = State(tmp_db, migrations_dir)
    st.init()
    item = make_item(link="https://example.com/news", summary="no doi here", title="News story")
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json={"data": []})
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
