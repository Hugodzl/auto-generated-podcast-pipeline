import textwrap
from pathlib import Path

import pytest

from rabbit_hole.config import Config, load_config


def write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


MINIMAL = """
    [schedule]
    cron = "0 7 * * MON"
    episodes_per_run = 5

    [ranking]
    recency_half_life_days = 30

    [[topics]]
    name = "AI studies"
    weight = 1.0
    keywords = ["large language model", "neural network"]

    [[sources]]
    name = "Nature"
    type = "rss"
    url = "https://www.nature.com/nature.rss"
    weight = 1.0

    [notebooklm]
    nlm_command = "nlm"

    [plex]
    base_url = "http://127.0.0.1:32400/"
    library_name = "The Random Rabbit Hole"
    target_folder = "D:/Videos/PlexLibrary/Podcasts/The Random Rabbit Hole"

    [apis]
    unpaywall_email = "you@example.com"

    [notifications]
    discord_webhook_keyring_key = "discord_webhook_url"
"""


def test_load_minimal_config(tmp_path):
    cfg = load_config(write_config(tmp_path, MINIMAL))
    assert isinstance(cfg, Config)
    assert cfg.schedule.episodes_per_run == 5
    assert cfg.ranking.recency_half_life_days == 30
    assert cfg.topics[0].name == "AI studies"
    assert cfg.sources[0].weight == 1.0
    assert cfg.apis.unpaywall_email == "you@example.com"


def test_source_enabled_defaults_true(tmp_path):
    cfg = load_config(write_config(tmp_path, MINIMAL))
    assert cfg.sources[0].enabled is True


def test_invalid_weight_rejected(tmp_path):
    bad = MINIMAL.replace("weight = 1.0\n    keywords", "weight = -1.0\n    keywords")
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, bad))


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_empty_topics_rejected(tmp_path):
    # Replace the single [[topics]] block with an explicit empty list
    body = MINIMAL.replace(
        """    [[topics]]
    name = "AI studies"
    weight = 1.0
    keywords = ["large language model", "neural network"]""",
        "    topics = []",
    )
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, body))


def test_notebooklm_audio_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path, MINIMAL))
    assert cfg.notebooklm.audio_format == "deep_dive"
    assert cfg.notebooklm.audio_length == "default"
    assert cfg.notebooklm.focus == ""


def test_notebooklm_audio_overrides_loaded(tmp_path):
    body = MINIMAL.replace(
        'nlm_command = "nlm"',
        'nlm_command = "nlm"\n'
        '    audio_format = "debate"\n'
        '    audio_length = "long"\n'
        '    focus = "methods"',
    )
    cfg = load_config(write_config(tmp_path, body))
    assert cfg.notebooklm.audio_format == "debate"
    assert cfg.notebooklm.audio_length == "long"
    assert cfg.notebooklm.focus == "methods"


def test_invalid_audio_length_rejected(tmp_path):
    body = MINIMAL.replace(
        'nlm_command = "nlm"',
        'nlm_command = "nlm"\n    audio_length = "verylong"',
    )
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, body))


def test_invalid_audio_format_rejected(tmp_path):
    body = MINIMAL.replace(
        'nlm_command = "nlm"',
        'nlm_command = "nlm"\n    audio_format = "podcast"',
    )
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, body))


def test_audio_gain_defaults_to_1_4(tmp_path):
    cfg = load_config(write_config(tmp_path, MINIMAL))
    assert cfg.audio.gain_factor == 1.4


def test_audio_gain_override_loaded(tmp_path):
    body = MINIMAL + '    [audio]\n    gain_factor = 1.8\n'
    cfg = load_config(write_config(tmp_path, body))
    assert cfg.audio.gain_factor == 1.8


def test_audio_gain_must_be_positive(tmp_path):
    body = MINIMAL + '    [audio]\n    gain_factor = 0\n'
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, body))
