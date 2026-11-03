"""The outbound User-Agent carries the operator's address, not the author's.

The address used to be a module constant in resolver.py and sources.py, which
put a real mailbox in a public repo. It now comes from `[apis] unpaywall_email`,
so these tests pin the two halves of that: load_config publishes the address,
and a process that never loaded a config still sends something valid.
"""

import textwrap
from pathlib import Path

import pytest

from rabbit_hole import useragent
from rabbit_hole.config import load_config

from tests.test_config import MINIMAL


@pytest.fixture(autouse=True)
def reset_contact():
    """set_contact writes module state; don't leak it into the next test."""
    before = useragent._contact
    yield
    useragent.set_contact(before)


def test_user_agent_identifies_the_operator_from_config(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(MINIMAL), encoding="utf-8")

    load_config(p)

    assert useragent.user_agent() == "RandomRabbitHole/1.0 (mailto:you@example.com)"


def test_user_agent_omits_the_mailto_when_no_config_was_loaded():
    useragent.set_contact("")
    # Not "(mailto:)" -- an empty contact is worse than no contact at all, and
    # scripts that use resolver/sources without a Config hit exactly this path.
    assert useragent.user_agent() == "RandomRabbitHole/1.0"


def test_no_personal_address_is_baked_into_the_source():
    """The regression this file exists for: a hardcoded mailbox coming back."""
    root = Path(__file__).resolve().parent.parent / "rabbit_hole"
    for py in root.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "mailto:" not in text or py.name == "useragent.py", (
            f"{py.name} hardcodes a contact address; it belongs in config"
        )
