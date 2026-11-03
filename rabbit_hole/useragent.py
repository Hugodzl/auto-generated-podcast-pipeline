"""The polite-crawler identity this pipeline sends to feeds and paper APIs.

Unpaywall and Crossref ask clients to put a contact address in the User-Agent so
they can reach the operator about a misbehaving client, and publisher PDF hosts
are friendlier to a request that identifies itself. The address belongs to
whoever runs the pipeline, so it comes from config (`[apis] unpaywall_email`)
instead of being baked into the source -- this repo is public, and a mailbox
hardcoded in a public repo is a spam target.

`load_config()` calls `set_contact()` once at startup. Until it does -- in tests,
or in a script that never loads a config -- `user_agent()` falls back to the bare
product token, which is what an unidentified client would have sent anyway.
"""

_PRODUCT = "RandomRabbitHole/1.0"

_contact = ""


def set_contact(email: str) -> None:
    """Record the operator's contact address. Idempotent; last call wins."""
    global _contact
    _contact = (email or "").strip()


def user_agent() -> str:
    return f"{_PRODUCT} (mailto:{_contact})" if _contact else _PRODUCT
