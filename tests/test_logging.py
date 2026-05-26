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
