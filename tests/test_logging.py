import respx
import httpx

from rabbit_hole.logging_config import post_discord


@respx.mock
def test_post_discord_sends_payload():
    route = respx.post("https://discord.test/webhook").mock(
        return_value=httpx.Response(204)
    )
    post_discord("https://discord.test/webhook", "something broke")
    assert route.called
    sent = route.calls[0].request
    assert b"something broke" in sent.content


def test_post_discord_never_raises_on_bad_url():
    # Empty or None webhook must not raise — alerting must never crash the run.
    post_discord("", "msg")
    post_discord(None, "msg")  # type: ignore[arg-type]


def test_configure_logging_returns_usable_logger(tmp_path):
    from rabbit_hole.logging_config import configure_logging

    log_dir = tmp_path / "logs"
    logger = configure_logging(log_dir, webhook_url=None)
    assert log_dir.exists()
    # Must accept structured kwargs at INFO and ERROR without raising.
    logger.info("smoke", count=3)
    logger.error("oops", error="boom")


def test_configure_logging_writes_json_events_to_utf8_file(tmp_path):
    # The headless scheduled run has no console, so structured events MUST land
    # in rabbit_hole.log (not stdout), and the file MUST be UTF-8 so non-ASCII
    # paper titles don't crash logging on a cp1252 Windows host.
    import logging

    from rabbit_hole.logging_config import configure_logging

    log_dir = tmp_path / "logs"
    logger = configure_logging(log_dir, webhook_url=None)
    logger.info("filed", title="Café déjà vu ✓ 你好")
    for h in logging.getLogger().handlers:
        h.flush()

    log_file = log_dir / "rabbit_hole.log"
    content = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    assert '"event": "filed"' in content
    assert "Café déjà vu ✓ 你好" in content
