"""Tests for the shared one-off/themed-episode machinery.

This module exists because the machinery used to live as four hand-copied blocks
under scripts/, where nothing tested it and fixes reached some copies and not
others -- `make_topic_episode.py` carried the known-wrong `_url_reachable` HTTP
check for three weeks after it was replaced elsewhere, and the User-Agent fix for
publisher PDF hosts landed in exactly one copy. The regression tests below pin
the behaviours that actually broke real runs.
"""
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from rabbit_hole import oneoff
from rabbit_hole.notebooklm import NotebookLMError
from rabbit_hole.oneoff import EpisodeSpec


class _NullLog:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def info(self, event, **k):
        self.events.append((event, k))

    def warning(self, event, **k):
        self.events.append((event, k))

    def error(self, event, **k):
        self.events.append((event, k))


def _spec(tmp_path: Path, **over) -> EpisodeSpec:
    kw = dict(
        slug="demo",
        label="demo",
        title="A Title — with an em dash",
        topic_bucket="Technology and society",
        pdf_dir=tmp_path / "pdfs",
        pdf_sources=(("01-a.pdf", "http://x/a.pdf"),),
        url_sources=("http://x/page",),
        focus="Do the thing.",
    )
    kw.update(over)
    return EpisodeSpec(**kw)


def _write_pdf(spec: EpisodeSpec, name: str, size: int = 20_000) -> Path:
    spec.pdf_dir.mkdir(parents=True, exist_ok=True)
    p = spec.pdf_dir / name
    p.write_bytes(b"%PDF-1.4" + b"x" * size)
    return p


def _ok(stdout="[]"):
    return subprocess.CompletedProcess(args=["nlm"], returncode=0, stdout=stdout, stderr="")


def _fail(stderr="nope"):
    return subprocess.CompletedProcess(args=["nlm"], returncode=1, stdout="", stderr=stderr)


# --------------------------------------------------------------------------
# sources / build_paper
# --------------------------------------------------------------------------

def test_sources_lists_files_before_urls(tmp_path):
    spec = _spec(tmp_path,
                 pdf_sources=(("01-a.pdf", "u1"), ("02-b.pdf", "u2")),
                 url_sources=("http://x/1", "http://x/2"))
    refs = oneoff.sources(spec)
    assert [r.kind for r in refs] == ["file", "file", "url", "url"]
    assert refs[0].value == str(spec.pdf_dir / "01-a.pdf")
    assert refs[3].value == "http://x/2"


def test_build_paper_carries_title_and_language(tmp_path):
    spec = _spec(tmp_path, language="fr")
    paper = oneoff.build_paper(spec)
    assert paper.title == spec.title
    assert paper.language == "fr"
    assert paper.pdf_path == spec.pdf_dir / "01-a.pdf"


def test_build_paper_raises_when_there_are_no_pdf_sources(tmp_path):
    """A URL-only reading list has no first PDF to stand in as pdf_path. Fail in
    build_paper so --check catches it, rather than at generate time."""
    spec = _spec(tmp_path, pdf_sources=())
    with pytest.raises(ValueError):
        oneoff.build_paper(spec)


# --------------------------------------------------------------------------
# fetch_missing_pdfs
# --------------------------------------------------------------------------

@respx.mock
def test_fetch_downloads_only_what_is_missing(tmp_path):
    spec = _spec(tmp_path, pdf_sources=(("01-a.pdf", "http://x/a.pdf"),
                                        ("02-b.pdf", "http://x/b.pdf")))
    _write_pdf(spec, "01-a.pdf")
    route = respx.get("http://x/b.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4" + b"y" * 20_000))
    assert oneoff.fetch_missing_pdfs(spec) == 1
    assert route.called
    assert (spec.pdf_dir / "02-b.pdf").exists()


@respx.mock
def test_fetch_redownloads_a_truncated_file(tmp_path):
    """A 300-byte 'PDF' is an error page someone saved; treat it as missing."""
    spec = _spec(tmp_path)
    spec.pdf_dir.mkdir(parents=True, exist_ok=True)
    (spec.pdf_dir / "01-a.pdf").write_bytes(b"%PDF-1.4 tiny")
    respx.get("http://x/a.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4" + b"z" * 20_000))
    assert oneoff.fetch_missing_pdfs(spec) == 1
    assert (spec.pdf_dir / "01-a.pdf").stat().st_size > 10_000


@respx.mock
def test_fetch_rejects_a_non_pdf_body(tmp_path):
    spec = _spec(tmp_path)
    respx.get("http://x/a.pdf").mock(
        return_value=httpx.Response(200, content=b"<!DOCTYPE html><html>nope"))
    with pytest.raises(RuntimeError, match="did not return a PDF"):
        oneoff.fetch_missing_pdfs(spec)
    assert not (spec.pdf_dir / "01-a.pdf").exists()


@respx.mock
def test_fetch_sends_a_browser_user_agent(tmp_path):
    """Regression: nature.com and frontiersin.org serve a real PDF to curl and an
    HTML interstitial to httpx's default agent, which surfaced as 'did not return
    a PDF'. The header is load-bearing, so pin it."""
    spec = _spec(tmp_path)
    route = respx.get("http://x/a.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4" + b"q" * 20_000))
    oneoff.fetch_missing_pdfs(spec)
    ua = route.calls[0].request.headers["user-agent"]
    assert "Mozilla/5.0" in ua
    assert "python-httpx" not in ua.lower()


# --------------------------------------------------------------------------
# urls_nlm_can_ingest
# --------------------------------------------------------------------------

def test_probe_reports_urls_nlm_refuses(monkeypatch):
    calls = []

    def fake_nlm(args, timeout):
        calls.append(args)
        if args[:2] == ["list", "notebooks"]:
            return _ok('[{"id": "nb1", "title": "rh-preflight probe", "updated_at": "z"}]')
        if args[:2] == ["source", "add"]:
            return _fail() if "http://bad" in args else _ok()
        return _ok()

    monkeypatch.setattr(oneoff, "_nlm", fake_nlm)
    monkeypatch.setattr(oneoff, "_probe_title", lambda: "rh-preflight probe")
    rejected, fatal = oneoff.urls_nlm_can_ingest(["http://good", "http://bad"])
    assert fatal is None
    assert rejected == ["http://bad"]


def test_probe_deletes_its_notebook_even_when_a_source_is_refused(monkeypatch):
    """The throwaway notebook is deleted in a finally. A leaked notebook is how a
    killed run leaves litter behind in the account."""
    deleted = []

    def fake_nlm(args, timeout):
        if args[:2] == ["list", "notebooks"]:
            return _ok('[{"id": "nb1", "title": "rh-preflight probe", "updated_at": "z"}]')
        if args[:2] == ["notebook", "delete"]:
            deleted.append(args[2])
            return _ok()
        if args[:2] == ["source", "add"]:
            return _fail()
        return _ok()

    monkeypatch.setattr(oneoff, "_nlm", fake_nlm)
    monkeypatch.setattr(oneoff, "_probe_title", lambda: "rh-preflight probe")
    oneoff.urls_nlm_can_ingest(["http://bad"])
    assert deleted == ["nb1"]


def test_probe_is_fatal_when_the_notebook_cannot_be_created(monkeypatch):
    monkeypatch.setattr(oneoff, "_nlm",
                        lambda args, timeout: _fail() if args[:2] == ["notebook", "create"] else _ok())
    rejected, fatal = oneoff.urls_nlm_can_ingest(["http://x"])
    assert rejected == []
    assert fatal and "probe notebook" in fatal


def test_probe_is_fatal_when_the_notebook_id_is_not_found(monkeypatch):
    monkeypatch.setattr(oneoff, "_nlm",
                        lambda args, timeout: _ok("[]") if args[:2] == ["list", "notebooks"] else _ok())
    monkeypatch.setattr(oneoff, "_probe_title", lambda: "rh-preflight probe")
    rejected, fatal = oneoff.urls_nlm_can_ingest(["http://x"])
    assert fatal and "id was not found" in fatal


def test_probe_survives_unparseable_notebook_list(monkeypatch):
    monkeypatch.setattr(oneoff, "_nlm",
                        lambda args, timeout: _ok("not json") if args[:2] == ["list", "notebooks"] else _ok())
    monkeypatch.setattr(oneoff, "_probe_title", lambda: "rh-preflight probe")
    _, fatal = oneoff.urls_nlm_can_ingest(["http://x"])
    assert fatal is not None


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

@pytest.fixture
def green(monkeypatch, tmp_path):
    """Everything a preflight needs, healthy. Individual tests break one thing."""
    monkeypatch.setattr(oneoff.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(oneoff.keyring, "get_password", lambda s, k: "tok")
    monkeypatch.setattr(oneoff, "_nlm", lambda args, timeout: _ok())
    monkeypatch.setattr(oneoff, "urls_nlm_can_ingest", lambda urls: ([], None))


def test_preflight_clean_when_everything_is_present(tmp_path, green):
    spec = _spec(tmp_path)
    _write_pdf(spec, "01-a.pdf")
    assert oneoff.preflight(spec) == []


def test_preflight_reports_a_missing_pdf(tmp_path, green):
    spec = _spec(tmp_path)
    problems = oneoff.preflight(spec)
    assert any("source PDF not found" in p for p in problems)


def test_preflight_reports_a_suspiciously_small_pdf(tmp_path, green):
    spec = _spec(tmp_path)
    spec.pdf_dir.mkdir(parents=True, exist_ok=True)
    (spec.pdf_dir / "01-a.pdf").write_bytes(b"%PDF tiny")
    assert any("suspiciously small" in p for p in oneoff.preflight(spec))


def test_preflight_reports_missing_ffmpeg(tmp_path, green, monkeypatch):
    spec = _spec(tmp_path)
    _write_pdf(spec, "01-a.pdf")
    monkeypatch.setattr(oneoff.shutil, "which", lambda t: None if t == "ffmpeg" else "/x")
    assert any("ffmpeg" in p for p in oneoff.preflight(spec))


def test_preflight_reports_missing_plex_token(tmp_path, green, monkeypatch):
    spec = _spec(tmp_path)
    _write_pdf(spec, "01-a.pdf")
    monkeypatch.setattr(oneoff.keyring, "get_password", lambda s, k: None)
    assert any("Plex token" in p for p in oneoff.preflight(spec))


def test_preflight_reports_metadata_that_cannot_be_built(tmp_path, green):
    """Regression: --check once reported OK while build_paper() still raised, so a
    detached run died seconds in with the preflight's blessing."""
    spec = _spec(tmp_path, pdf_sources=())
    assert any("episode metadata" in p for p in oneoff.preflight(spec))


def test_preflight_stops_before_probing_when_nlm_is_unauthenticated(tmp_path, green, monkeypatch):
    """No point probing 14 URLs with a dead login: every probe would fail and the
    report would bury the real cause under fourteen false ones."""
    spec = _spec(tmp_path)
    _write_pdf(spec, "01-a.pdf")
    monkeypatch.setattr(oneoff, "_nlm", lambda args, timeout: _fail())
    probed = []
    monkeypatch.setattr(oneoff, "urls_nlm_can_ingest",
                        lambda urls: (probed.append(urls), ([], None))[1])
    problems = oneoff.preflight(spec)
    assert any("not authenticated" in p for p in problems)
    assert probed == []


def test_preflight_reports_nlm_missing_from_path(tmp_path, green, monkeypatch):
    spec = _spec(tmp_path)
    _write_pdf(spec, "01-a.pdf")
    monkeypatch.setattr(oneoff, "_nlm", lambda args, timeout: None)
    assert any("not runnable" in p for p in oneoff.preflight(spec))


def test_preflight_surfaces_urls_the_probe_rejected(tmp_path, green, monkeypatch):
    spec = _spec(tmp_path)
    _write_pdf(spec, "01-a.pdf")
    monkeypatch.setattr(oneoff, "urls_nlm_can_ingest",
                        lambda urls: (["http://x/page"], None))
    problems = oneoff.preflight(spec)
    assert any("refuses to ingest" in p and "http://x/page" in p for p in problems)


def test_preflight_can_skip_the_probe(tmp_path, green, monkeypatch):
    spec = _spec(tmp_path)
    _write_pdf(spec, "01-a.pdf")
    monkeypatch.setattr(oneoff, "urls_nlm_can_ingest",
                        lambda urls: (["http://x/page"], None))
    assert oneoff.preflight(spec, probe_urls=False) == []


def test_preflight_does_not_probe_when_there_are_no_urls(tmp_path, green, monkeypatch):
    spec = _spec(tmp_path, url_sources=())
    _write_pdf(spec, "01-a.pdf")
    called = []
    monkeypatch.setattr(oneoff, "urls_nlm_can_ingest",
                        lambda urls: (called.append(1), ([], None))[1])
    assert oneoff.preflight(spec) == []
    assert called == []


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A run() whose external effects are all stubbed. Returns the recorder."""
    log = _NullLog()
    rec = {"log": log, "generate": [], "boost": [], "filed": [], "discord": []}

    monkeypatch.setattr(oneoff, "configure_logging", lambda *a, **k: log)
    monkeypatch.setattr(oneoff.keyring, "get_password", lambda s, k: "tok")
    monkeypatch.setattr(oneoff, "post_discord",
                        lambda hook, msg: rec["discord"].append(msg))
    monkeypatch.setattr(oneoff, "load_config", lambda p: _FakeConfig())

    class _Ep:
        audio_path = tmp_path / "a.m4a"

    def fake_generate(paper, cfg, out_dir, **kw):
        rec["generate"].append((paper, kw))
        return _Ep()

    monkeypatch.setattr(oneoff, "generate", fake_generate)
    monkeypatch.setattr(oneoff, "boost_loudness",
                        lambda path, gain: rec["boost"].append(gain))
    monkeypatch.setattr(oneoff, "file_episode",
                        lambda ep, bucket, plex, token: rec["filed"].append(bucket)
                        or tmp_path / "Bucket - Title.m4a")
    return rec


class _FakeConfig:
    class audio:
        gain_factor = 1.4
    notebooklm = object()
    plex = object()


def test_run_returns_2_and_reports_when_preflight_fails(tmp_path, wired, monkeypatch, capsys):
    spec = _spec(tmp_path)
    monkeypatch.setattr(oneoff, "preflight", lambda s, probe_urls=True: ["boom"])
    assert oneoff.run(spec, []) == 2
    assert "PREFLIGHT FAILED" in capsys.readouterr().out
    assert wired["generate"] == []


def test_run_check_stops_before_generating(tmp_path, wired, monkeypatch):
    spec = _spec(tmp_path)
    monkeypatch.setattr(oneoff, "preflight", lambda s, probe_urls=True: [])
    assert oneoff.run(spec, ["--check"]) == 0
    assert wired["generate"] == []


def test_run_no_probe_flag_reaches_preflight(tmp_path, wired, monkeypatch):
    spec = _spec(tmp_path)
    seen = {}
    monkeypatch.setattr(oneoff, "preflight",
                        lambda s, probe_urls=True: seen.setdefault("probe", probe_urls) and [] or [])
    oneoff.run(spec, ["--check", "--no-probe"])
    assert seen["probe"] is False


def test_run_fetch_flag_downloads_first(tmp_path, wired, monkeypatch):
    spec = _spec(tmp_path)
    fetched = []
    monkeypatch.setattr(oneoff, "fetch_missing_pdfs",
                        lambda s: fetched.append(s.slug) or 1)
    monkeypatch.setattr(oneoff, "preflight", lambda s, probe_urls=True: [])
    oneoff.run(spec, ["--fetch", "--check"])
    assert fetched == ["demo"]


def test_run_happy_path_generates_boosts_and_files(tmp_path, wired, monkeypatch):
    spec = _spec(tmp_path)
    monkeypatch.setattr(oneoff, "preflight", lambda s, probe_urls=True: [])
    assert oneoff.run(spec, []) == 0
    (paper, kw) = wired["generate"][0]
    assert paper.title == spec.title
    assert kw["focus"] == spec.focus
    assert kw["audio_length"] == "long"
    assert len(kw["sources"]) == 2          # one PDF + one URL
    assert wired["boost"] == [1.4]
    assert wired["filed"] == ["Technology and society"]


def test_run_files_the_episode_even_when_the_boost_fails(tmp_path, wired, monkeypatch):
    """Mirrors _publish: a quiet episode beats a dropped one."""
    from rabbit_hole.audio import AudioBoostError
    spec = _spec(tmp_path)
    monkeypatch.setattr(oneoff, "preflight", lambda s, probe_urls=True: [])

    def boom(path, gain):
        raise AudioBoostError("ffmpeg died")

    monkeypatch.setattr(oneoff, "boost_loudness", boom)
    assert oneoff.run(spec, []) == 0
    assert wired["filed"] == ["Technology and society"]


def test_run_returns_1_when_notebooklm_fails(tmp_path, wired, monkeypatch):
    spec = _spec(tmp_path)
    monkeypatch.setattr(oneoff, "preflight", lambda s, probe_urls=True: [])

    def boom(*a, **k):
        raise NotebookLMError("nlm exploded")

    monkeypatch.setattr(oneoff, "generate", boom)
    assert oneoff.run(spec, []) == 1
    assert wired["filed"] == []
    assert any("FAILED at NotebookLM" in m for m in wired["discord"])


def test_run_log_events_are_namespaced_by_slug(tmp_path, wired, monkeypatch):
    spec = _spec(tmp_path, slug="knowledge_system")
    monkeypatch.setattr(oneoff, "preflight", lambda s, probe_urls=True: [])
    oneoff.run(spec, [])
    events = [e for e, _ in wired["log"].events]
    assert "knowledge_system_episode_filed" in events
    assert "knowledge_system_episode_generated" in events


def test_run_prints_an_ascii_safe_done_line(tmp_path, wired, monkeypatch, capsys):
    """The titles carry em dashes and a Scheduled Task's stdout is not UTF-8. The
    episode is already filed by then, so printing must never be what fails."""
    spec = _spec(tmp_path)
    monkeypatch.setattr(oneoff, "preflight", lambda s, probe_urls=True: [])
    monkeypatch.setattr(oneoff, "file_episode",
                        lambda *a: tmp_path / "Bucket - A Title — dash.m4a")
    oneoff.run(spec, [])
    out = capsys.readouterr().out
    assert "DONE:" in out
    out.encode("ascii")   # raises if a non-ascii char slipped through
