import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rabbit_hole import notebooklm
from rabbit_hole.config import NotebookLMConfig
from rabbit_hole.notebooklm import GeneratedEpisode, generate, NotebookLMError
from rabbit_hole.resolver import Paper
from rabbit_hole.sources import FeedItem


def make_paper(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    item = FeedItem(source_name="Nature", source_weight=1.0, title="T",
                    summary="s", link="http://x",
                    published_at=datetime(2026, 5, 19, tzinfo=timezone.utc), raw={})
    return Paper(doi="10.1/x", title="T", authors=[], abstract="s",
                 published_at=item.published_at, pdf_path=pdf, language="en", source=item)


def test_generate_success(tmp_path, monkeypatch):
    paper = make_paper(tmp_path)
    audio = tmp_path / "out.m4a"
    audio.write_bytes(b"fake audio")

    def fake_run_nlm(cmd, pdf_path, out_dir, **kwargs):
        return audio  # pretend nlm produced this file

    monkeypatch.setattr("rabbit_hole.notebooklm._run_nlm", fake_run_nlm)
    ep = generate(paper, NotebookLMConfig(nlm_command="nlm"), out_dir=tmp_path)
    assert isinstance(ep, GeneratedEpisode)
    assert ep.audio_path == audio
    assert ep.paper is paper


def test_generate_raises_when_no_output(tmp_path, monkeypatch):
    paper = make_paper(tmp_path)
    monkeypatch.setattr("rabbit_hole.notebooklm._run_nlm", lambda *a, **k: None)
    with pytest.raises(NotebookLMError):
        generate(paper, NotebookLMConfig(nlm_command="nlm"), out_dir=tmp_path)


class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class FakeNlm:
    """Simulate the multi-step nlm CLI, dispatching on argv.

    Lets _run_nlm tests exercise the real orchestration and JSON parsing
    instead of a single mocked call.
    """

    def __init__(self, *, statuses=("completed",), create_rc=0, add_rc=0,
                 audio_rc=0, download_rc=0, make_file=True, download_fail_times=0):
        self.statuses = list(statuses)
        self.create_rc = create_rc
        self.add_rc = add_rc
        self.audio_rc = audio_rc
        self.download_rc = download_rc
        self.make_file = make_file
        self.download_fail_times = download_fail_times
        self.notebook_id = "nb-abc-123"
        self.artifact_id = "art-xyz-789"
        self._title = None
        self._polls = 0
        self._download_calls = 0
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None, **kwargs):
        self.calls.append(cmd)
        parts = cmd[1:]
        if parts[:2] == ["notebook", "create"]:
            self._title = parts[2]
            return _Result(self.create_rc)
        if parts[:2] == ["list", "notebooks"]:
            data = []
            if self._title is not None and self.create_rc == 0:
                data = [{"id": self.notebook_id, "title": self._title,
                         "source_count": 1, "updated_at": "2026-05-27T00:00:00Z"}]
            return _Result(0, json.dumps(data))
        if parts[:2] == ["source", "add"]:
            return _Result(self.add_rc)
        if parts[:2] == ["audio", "create"]:
            return _Result(self.audio_rc)
        if parts[:2] == ["status", "artifacts"]:
            status = self.statuses[min(self._polls, len(self.statuses) - 1)]
            self._polls += 1
            return _Result(0, json.dumps(
                [{"id": self.artifact_id, "type": "audio", "status": status}]))
        if parts[:2] == ["download", "audio"]:
            self._download_calls += 1
            if self._download_calls <= self.download_fail_times:
                return _Result(1)  # rendition not served yet (404); no file written
            if self.download_rc == 0 and self.make_file:
                out = Path(cmd[cmd.index("--output") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"audio-bytes")
            return _Result(self.download_rc)
        if parts[:2] == ["notebook", "delete"]:
            return _Result(0)
        return _Result(0)

    def deleted_notebooks(self):
        return [c[3] for c in self.calls if c[1:3] == ["notebook", "delete"]]


def _install_fake(monkeypatch, fake):
    monkeypatch.setattr(notebooklm.subprocess, "run",
                        lambda cmd, **kw: fake(cmd, **kw))
    monkeypatch.setattr(notebooklm, "_AUDIO_POLL_INTERVAL", 0, raising=False)
    monkeypatch.setattr(notebooklm, "_DOWNLOAD_RETRY_INTERVAL", 0, raising=False)


def _audio_create_cmd(fake):
    return next(c for c in fake.calls if c[1:3] == ["audio", "create"])


def _flag_value(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def test_run_nlm_audio_create_uses_given_format_and_length(tmp_path, monkeypatch):
    fake = FakeNlm()
    _install_fake(monkeypatch, fake)

    notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o",
                        audio_format="debate", audio_length="long", focus="")

    cmd = _audio_create_cmd(fake)
    assert _flag_value(cmd, "--format") == "debate"
    assert _flag_value(cmd, "--length") == "long"
    assert "--focus" not in cmd
    assert cmd[-1] == "--confirm"


def test_run_nlm_audio_create_includes_focus_when_set(tmp_path, monkeypatch):
    fake = FakeNlm()
    _install_fake(monkeypatch, fake)

    notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o",
                        focus="methods and limitations")

    cmd = _audio_create_cmd(fake)
    assert _flag_value(cmd, "--focus") == "methods and limitations"
    assert cmd[-1] == "--confirm"   # --confirm stays last even after --focus is appended


def test_run_nlm_audio_create_defaults_match_cli(tmp_path, monkeypatch):
    fake = FakeNlm()
    _install_fake(monkeypatch, fake)

    notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o")

    cmd = _audio_create_cmd(fake)
    assert _flag_value(cmd, "--format") == "deep_dive"
    assert _flag_value(cmd, "--length") == "default"
    assert "--focus" not in cmd
    assert cmd[-1] == "--confirm"


def test_run_nlm_happy_path_downloads_audio_and_cleans_up(tmp_path, monkeypatch):
    fake = FakeNlm(statuses=("completed",))
    _install_fake(monkeypatch, fake)
    out = tmp_path / "audio"

    result = notebooklm._run_nlm("nlm", tmp_path / "p.pdf", out)

    assert result == out / f"{fake.notebook_id}_audio.m4a"
    assert result.exists()
    subcmds = [tuple(c[1:3]) for c in fake.calls]
    assert ("notebook", "create") in subcmds
    assert ("source", "add") in subcmds
    assert ("audio", "create") in subcmds
    assert ("download", "audio") in subcmds
    assert fake.deleted_notebooks() == [fake.notebook_id]


def test_run_nlm_adds_pdf_as_file_source_and_waits(tmp_path, monkeypatch):
    fake = FakeNlm()
    _install_fake(monkeypatch, fake)
    pdf = tmp_path / "p.pdf"

    notebooklm._run_nlm("nlm", pdf, tmp_path / "o")

    add = next(c for c in fake.calls if c[1:3] == ["source", "add"])
    assert "--file" in add and str(pdf) in add
    assert "--wait" in add


def test_run_nlm_polls_until_audio_completed(tmp_path, monkeypatch):
    fake = FakeNlm(statuses=("generating", "generating", "completed"))
    _install_fake(monkeypatch, fake)

    result = notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o")

    assert result is not None
    polls = [c for c in fake.calls if c[1:3] == ["status", "artifacts"]]
    assert len(polls) == 3


def test_run_nlm_none_when_notebook_create_fails(tmp_path, monkeypatch):
    fake = FakeNlm(create_rc=1)
    _install_fake(monkeypatch, fake)

    assert notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o") is None
    assert any(c[1:3] == ["notebook", "create"] for c in fake.calls)
    assert fake.deleted_notebooks() == []


def test_run_nlm_times_out_when_audio_never_completes(tmp_path, monkeypatch):
    fake = FakeNlm(statuses=("generating",))
    _install_fake(monkeypatch, fake)
    monkeypatch.setattr(notebooklm, "_AUDIO_POLL_TIMEOUT", 0, raising=False)

    result = notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o")

    assert result is None
    assert fake.deleted_notebooks() == [fake.notebook_id]


def test_run_nlm_none_and_cleans_up_when_download_fails(tmp_path, monkeypatch):
    fake = FakeNlm(download_rc=1)
    _install_fake(monkeypatch, fake)

    assert notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o") is None
    assert fake.deleted_notebooks() == [fake.notebook_id]


def test_run_nlm_retries_download_until_rendition_ready(tmp_path, monkeypatch):
    # nlm reports the audio 'completed' before its media files are downloadable
    # (they 404 until the artifact is fully ready), so the first download attempts
    # fail and _run_nlm must retry until one succeeds.
    fake = FakeNlm(download_fail_times=2)
    _install_fake(monkeypatch, fake)
    out = tmp_path / "audio"

    result = notebooklm._run_nlm("nlm", tmp_path / "p.pdf", out)

    assert result == out / f"{fake.notebook_id}_audio.m4a"
    assert result.exists()
    dl_calls = [c for c in fake.calls if c[1:3] == ["download", "audio"]]
    assert len(dl_calls) == 3  # two 404s, then success


def test_run_nlm_none_on_timeout(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(notebooklm.subprocess, "run", boom)
    assert notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o") is None


def test_run_nlm_none_when_command_missing(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("nlm not on PATH")

    monkeypatch.setattr(notebooklm.subprocess, "run", boom)
    assert notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path / "o") is None


def test_nlm_decodes_nlm_utf8_output_not_locale():
    # nlm emits UTF-8 (e.g. its "✓" decorations). _nlm must decode as UTF-8,
    # not the platform locale (cp1252 on Windows), whose undefined byte 0x8f
    # otherwise crashes the subprocess reader thread and nulls out stdout.
    raw = b"\xe2\x9c\x93 ok \xc2\x8f"
    code = f"import sys; sys.stdout.buffer.write(bytes({list(raw)}))"
    result = notebooklm._nlm(sys.executable, ["-c", code], timeout=30)
    assert result is not None
    assert result.returncode == 0
    assert result.stdout == raw.decode("utf-8")


def test_generate_passes_config_audio_params_to_run_nlm(tmp_path, monkeypatch):
    paper = make_paper(tmp_path)
    audio = tmp_path / "out.m4a"
    audio.write_bytes(b"a")
    captured = {}

    def fake_run_nlm(cmd, pdf_path, out_dir, *, audio_format, audio_length, focus):
        captured.update(audio_format=audio_format, audio_length=audio_length, focus=focus)
        return audio

    monkeypatch.setattr("rabbit_hole.notebooklm._run_nlm", fake_run_nlm)
    generate(paper, NotebookLMConfig(audio_format="brief", audio_length="long", focus="X"),
             out_dir=tmp_path)
    assert captured == {"audio_format": "brief", "audio_length": "long", "focus": "X"}


def test_generate_override_takes_precedence_over_config(tmp_path, monkeypatch):
    paper = make_paper(tmp_path)
    audio = tmp_path / "out.m4a"
    audio.write_bytes(b"a")
    captured = {}

    def fake_run_nlm(cmd, pdf_path, out_dir, *, audio_format, audio_length, focus):
        captured.update(audio_format=audio_format, audio_length=audio_length, focus=focus)
        return audio

    monkeypatch.setattr("rabbit_hole.notebooklm._run_nlm", fake_run_nlm)
    generate(paper, NotebookLMConfig(), out_dir=tmp_path, audio_length="long")
    assert captured["audio_length"] == "long"        # override wins
    assert captured["audio_format"] == "deep_dive"    # config default
    assert captured["focus"] == ""                    # config default


# ---------------------------------------------------------------------------
# Heartbeat / structured-event tests
# ---------------------------------------------------------------------------

from structlog.testing import capture_logs  # noqa: E402


def _fake_nlm_heartbeat(cmd, args, timeout):
    if args[:2] == ["download", "audio"]:
        Path(args[args.index("--output") + 1]).write_bytes(b"x")
    return _Result()


def test_run_nlm_emits_heartbeat_events(monkeypatch, tmp_path):
    monkeypatch.setattr(notebooklm, "_nlm", _fake_nlm_heartbeat)
    monkeypatch.setattr(notebooklm, "_find_notebook_id", lambda c, t: "nb1")
    monkeypatch.setattr(notebooklm, "_wait_for_audio", lambda c, n: "art1")
    with capture_logs() as logs:
        out = notebooklm._run_nlm("nlm", tmp_path / "p.pdf", tmp_path)
    assert out is not None
    events = [entry["event"] for entry in logs]
    for expected in ("nlm_notebook_created", "nlm_source_added",
                     "nlm_audio_requested", "nlm_audio_ready",
                     "nlm_notebook_deleted"):
        assert expected in events, f"missing {expected} in {events}"


def test_wait_for_audio_emits_waiting_tick(monkeypatch):
    responses = iter([[]] * 5 + [[{"type": "audio", "status": "completed", "id": "a1"}]])
    monkeypatch.setattr(notebooklm, "_nlm_json", lambda c, a, t: next(responses))
    monkeypatch.setattr(notebooklm.time, "sleep", lambda s: None)
    with capture_logs() as logs:
        assert notebooklm._wait_for_audio("nlm", "nb1") == "a1"
    assert "nlm_waiting" in [entry["event"] for entry in logs]
