import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

from rabbit_hole.config import NotebookLMConfig
from rabbit_hole.resolver import Paper
from rabbit_hole.state import hash_str

_CALL_TIMEOUT = 120            # seconds for a single non-waiting nlm call
_SOURCE_WAIT_TIMEOUT = 600     # seconds to wait for PDF ingest (`source add --wait`)
_AUDIO_POLL_INTERVAL = 15      # seconds between studio-artifact status polls
_AUDIO_POLL_TIMEOUT = 1800     # max seconds to wait for audio generation
_DOWNLOAD_RETRY_INTERVAL = 30  # seconds between download attempts
_DOWNLOAD_MAX_ATTEMPTS = 30    # ~15 min total: ride out the status->rendition lag

_log = structlog.get_logger()


class NotebookLMError(RuntimeError):
    pass


class NotebookLMRateLimited(NotebookLMError):
    """The account's audio-overview allowance is spent.

    Distinct from a per-paper failure: retrying with a different source cannot
    help, so the caller should stop asking rather than spend its failure budget.
    """


# nlm surfaces this as gRPC code 8 (RESOURCE_EXHAUSTED) wrapped in a Tailwind
# UserDisplayableError. Its prose says "wait a few minutes", but the cap
# observed on 2026-09-07 outlasted the run by hours -- treat it as "not today".
_RATE_LIMIT_MARKERS = ("code 8", "rate limit", "resource_exhausted", "quota")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


@dataclass
class GeneratedEpisode:
    paper: Paper
    audio_path: Path
    duration_seconds: int
    nlm_notebook_id: str


@dataclass(frozen=True)
class SourceRef:
    """One thing to feed a notebook: a local file or a public URL.

    The weekly pipeline always has exactly one source (the resolved paper PDF);
    a themed one-off episode is built from a whole reading list, so `_run_nlm`
    takes either shape.
    """
    kind: str    # "file" | "url"
    value: str

    @property
    def flag(self) -> str:
        return "--file" if self.kind == "file" else "--url"


def _normalize_sources(sources) -> tuple[list[SourceRef], str]:
    """Accept a single PDF path (the pipeline's shape) or a list of SourceRefs,
    and return the refs plus a label for the throwaway notebook's title."""
    if isinstance(sources, (str, Path)):
        path = Path(sources)
        return [SourceRef("file", str(path))], path.stem
    refs = list(sources)
    first_file = next((r for r in refs if r.kind == "file"), None)
    label = Path(first_file.value).stem if first_file else "topic"
    return refs, label


def nlm_label(pdf_path: str | Path | None, link: str) -> str:
    """The token in a notebook title that ties a running generation back to its
    pending_generations row, so recovery can adopt an orphan after a crash.

    A PDF-backed run uses the file stem (what _normalize_sources derives on its
    own); a url-sourced run has no file, so it uses the hash of the link -- the
    same name resolve() gives a PDF it cannot key by DOI.
    """
    return Path(pdf_path).stem if pdf_path else hash_str(link)


def _nlm(nlm_command: str, args: list[str], timeout: float):
    """Run one nlm subcommand. Returns the CompletedProcess, or None if the
    binary is missing or the call times out."""
    try:
        return subprocess.run(
            [nlm_command, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _cli_output(result) -> str:
    """Everything the CLI said, as one flat line of plain text.

    nlm prints its errors to STDOUT (a rich console), not stderr, hard-wraps
    them mid-word, and mixes in spinner frames and ANSI codes -- so read both
    streams, then strip the console decoration. The rate-limit match and the
    log line both want prose, and a stray braille spinner frame would blow up
    on a cp1252 console downstream.
    """
    if result is None:
        return "nlm did not run (binary missing or call timed out)"
    text = _ANSI_RE.sub("", (result.stdout or "") + " " + (result.stderr or ""))
    plain = "".join(c for c in text
                    if c.isprintable() and not "\u2800" <= c <= "\u28ff")
    return " ".join(plain.split())


def _is_rate_limited(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _RATE_LIMIT_MARKERS)


def _log_call_failed(step: str, result, **ctx) -> str:
    """Record what the CLI said about a failed step and return that text.

    The orchestrator only ever saw generate()'s "produced no audio", which hid
    the real cause for a week (2026-09-07); every failing step now says why.
    """
    output = _cli_output(result)
    _log.error("nlm_call_failed", step=step,
               returncode=None if result is None else result.returncode,
               output=output[:800], **ctx)
    return output


def _nlm_json(nlm_command: str, args: list[str], timeout: float):
    """Run an nlm subcommand expected to emit JSON on success; return the parsed
    value, or None on any failure."""
    result = _nlm(nlm_command, args, timeout)
    if result is None or result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        return None


def _find_notebook_id(nlm_command: str, title: str) -> str | None:
    notebooks = _nlm_json(nlm_command, ["list", "notebooks", "--json"], _CALL_TIMEOUT)
    if not isinstance(notebooks, list):
        return None
    matches = [n for n in notebooks if n.get("title") == title and n.get("id")]
    matches.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    return matches[0]["id"] if matches else None


def _wait_for_audio(nlm_command: str, notebook_id: str) -> str | None:
    """Poll studio artifacts until the audio overview is completed. Returns the
    audio artifact id, or None if it does not complete before the timeout."""
    started = time.monotonic()
    deadline = started + _AUDIO_POLL_TIMEOUT
    polls = 0
    while True:
        artifacts = _nlm_json(
            nlm_command, ["status", "artifacts", notebook_id, "--json"], _CALL_TIMEOUT
        )
        if isinstance(artifacts, list):
            for art in artifacts:
                if art.get("type") == "audio" and art.get("status") == "completed":
                    return art.get("id")
        polls += 1
        if polls % 4 == 0:  # one tick per minute at the 15s interval
            _log.info("nlm_waiting", notebook_id=notebook_id,
                      elapsed_s=round(time.monotonic() - started))
        if time.monotonic() >= deadline:
            return None
        time.sleep(_AUDIO_POLL_INTERVAL)


def _download_audio(nlm_command: str, notebook_id: str, artifact_id: str,
                    out_path: Path) -> bool:
    """Download the audio overview, retrying until the rendition is served.

    nlm normalizes the studio status to 'completed' as soon as the artifact
    reaches status code 2, but the media files 404 until it reaches code 3 a few
    minutes later. `status artifacts --json` can't distinguish the two, so
    _wait_for_audio returns early; retry the download until an attempt produces
    the file (or the attempt budget is exhausted)."""
    for attempt in range(_DOWNLOAD_MAX_ATTEMPTS):
        result = _nlm(
            nlm_command,
            ["download", "audio", notebook_id, "--id", artifact_id,
             "--output", str(out_path), "--no-progress"],
            _CALL_TIMEOUT,
        )
        if result is not None and result.returncode == 0 and out_path.exists():
            return True
        if attempt < _DOWNLOAD_MAX_ATTEMPTS - 1:
            _log.warning("nlm_download_retry", notebook_id=notebook_id,
                         attempt=attempt + 1, output=_cli_output(result)[:200])
            time.sleep(_DOWNLOAD_RETRY_INTERVAL)
    _log.error("nlm_download_failed", notebook_id=notebook_id,
               attempts=_DOWNLOAD_MAX_ATTEMPTS)
    return False


def _run_nlm(nlm_command: str, sources, out_dir: Path, *,
             audio_format: str = "deep_dive", audio_length: str = "default",
             focus: str = "", language: str = "en",
             label: str | None = None) -> Path | None:
    """Generate an audio overview from one or more sources via the nlm CLI.

    `sources` is either a single PDF path (the weekly pipeline's shape) or a list
    of SourceRefs for a themed multi-source episode.

    Multi-step: create a notebook, add each source (waiting for ingest), request
    an audio overview, poll until it completes, then download the .m4a. The
    per-run notebook is deleted afterward. Any failure — non-zero exit, timeout,
    missing binary, or generation never completing — returns None so the
    orchestrator's failure budget handles them uniformly, and logs what the CLI
    said (`nlm_call_failed`). The one exception is a spent audio allowance,
    which raises NotebookLMRateLimited: no other paper would fare better.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    refs, derived_label = _normalize_sources(sources)
    title = f"rabbit-hole {label or derived_label} {uuid.uuid4().hex[:8]}"
    notebook_id: str | None = None
    try:
        created = _nlm(nlm_command, ["notebook", "create", title], _CALL_TIMEOUT)
        if created is None or created.returncode != 0:
            _log_call_failed("notebook create", created, title=title)
            return None
        notebook_id = _find_notebook_id(nlm_command, title)
        if not notebook_id:
            return None
        _log.info("nlm_notebook_created", notebook_id=notebook_id, sources=len(refs))

        # Added one at a time rather than as one bulk call: a bulk add reports a
        # single exit code, so one dead link would be indistinguishable from a
        # clean run and we'd narrate an episode missing a source.
        for i, ref in enumerate(refs, start=1):
            added = _nlm(
                nlm_command,
                ["source", "add", notebook_id, ref.flag, ref.value,
                 "--wait", "--wait-timeout", str(_SOURCE_WAIT_TIMEOUT)],
                _SOURCE_WAIT_TIMEOUT + _CALL_TIMEOUT,
            )
            if added is None or added.returncode != 0:
                _log_call_failed("source add", added, notebook_id=notebook_id,
                                 source=ref.value)
                return None
            _log.info("nlm_source_added", notebook_id=notebook_id,
                      source=ref.value, index=i, total=len(refs))

        # --language is passed explicitly on purpose: it overrides the account /
        # NOTEBOOKLM_HL default so output language follows the paper, not the env.
        audio_args = ["audio", "create", notebook_id,
                      "--format", audio_format, "--length", audio_length,
                      "--language", language]
        if focus:
            audio_args += ["--focus", focus]
        audio_args.append("--confirm")
        requested = _nlm(nlm_command, audio_args, _CALL_TIMEOUT)
        if requested is None or requested.returncode != 0:
            output = _log_call_failed("audio create", requested, notebook_id=notebook_id)
            if _is_rate_limited(output):
                # Raise rather than return None: the next paper would hit the
                # same wall, so the caller must stop the run, not retry.
                # Short on purpose: this text lands in the Discord summary and
                # the run row. The CLI's own words are in nlm_call_failed above.
                raise NotebookLMRateLimited(
                    "nlm rate limited audio creation (per-account audio-overview "
                    "cap) — see the nlm_call_failed log line for the CLI text")
            return None
        _log.info("nlm_audio_requested", notebook_id=notebook_id)

        artifact_id = _wait_for_audio(nlm_command, notebook_id)
        if not artifact_id:
            _log.error("nlm_audio_never_completed", notebook_id=notebook_id,
                       waited_s=_AUDIO_POLL_TIMEOUT)
            return None
        _log.info("nlm_audio_ready", notebook_id=notebook_id, artifact_id=artifact_id)

        out_path = out_dir / f"{notebook_id}_audio.m4a"
        if _download_audio(nlm_command, notebook_id, artifact_id, out_path):
            return out_path
        return None
    finally:
        if notebook_id:
            deleted = _nlm(nlm_command, ["notebook", "delete", notebook_id, "--confirm"],
                           _CALL_TIMEOUT)
            if deleted is not None and deleted.returncode == 0:
                _log.info("nlm_notebook_deleted", notebook_id=notebook_id)
            else:
                # The orphan sweep at next startup will adopt-or-delete it.
                _log.warning("nlm_notebook_delete_failed", notebook_id=notebook_id)


def generate(paper: Paper, config: NotebookLMConfig, out_dir: Path, *,
             audio_format: str | None = None, audio_length: str | None = None,
             focus: str | None = None,
             sources: list[SourceRef] | None = None,
             label: str | None = None) -> GeneratedEpisode:
    """Turn a Paper into an episode. `sources` overrides the paper's own PDF for
    themed one-offs built from a reading list; the Paper is still carried through
    for titling and tagging."""
    fmt = audio_format if audio_format is not None else config.audio_format
    length = audio_length if audio_length is not None else config.audio_length
    foc = focus if focus is not None else config.focus
    audio = _run_nlm(config.nlm_command,
                     sources if sources is not None else paper.pdf_path,
                     Path(out_dir),
                     audio_format=fmt, audio_length=length, focus=foc,
                     language=paper.language, label=label)
    if audio is None or not audio.exists():
        raise NotebookLMError(f"nlm produced no audio for {paper.title!r}")
    return GeneratedEpisode(
        paper=paper,
        audio_path=audio,
        duration_seconds=0,   # populated by the plex tagging step via mutagen
        nlm_notebook_id="",   # the per-run notebook is deleted after download
    )
