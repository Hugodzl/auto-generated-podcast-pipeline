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
