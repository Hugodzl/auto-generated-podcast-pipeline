from datetime import datetime, timezone
from pathlib import Path

from rabbit_hole.notebooklm import GeneratedEpisode
from rabbit_hole.plex import episode_filename, sanitize
from rabbit_hole.resolver import Paper
from rabbit_hole.sources import FeedItem


def make_ep(tmp_path, title="Sperm whales have vowels"):
    audio = tmp_path / "tmp.m4a"
    audio.write_bytes(b"audio")
    item = FeedItem(source_name="Nature", source_weight=1.0, title=title, summary="s",
                    link="http://x", published_at=datetime(2026, 5, 19, tzinfo=timezone.utc), raw={})
    paper = Paper(doi="10.1/x", title=title, authors=[], abstract="s",
                  published_at=item.published_at, pdf_path=tmp_path / "p.pdf",
                  language="en", source=item)
    return GeneratedEpisode(paper=paper, audio_path=audio, duration_seconds=1200, nlm_notebook_id="n1")


def test_sanitize_strips_illegal_chars():
    assert sanitize('a/b:c*d?') == "a-b-c-d"


def test_episode_filename_uses_topic_not_date(tmp_path):
    ep = make_ep(tmp_path)
    name = episode_filename(ep, topic_bucket="Animal behavior", existing=set())
    assert name == "Animal behavior - Sperm whales have vowels.m4a"


def test_episode_filename_dedups_collision(tmp_path):
    ep = make_ep(tmp_path)
    existing = {"Animal behavior - Sperm whales have vowels.m4a"}
    name = episode_filename(ep, topic_bucket="Animal behavior", existing=existing)
    assert name == "Animal behavior - Sperm whales have vowels (2).m4a"


def test_episode_filename_falls_back_when_title_empty(tmp_path):
    # A title made entirely of illegal chars sanitizes to "" -> must fall back to the DOI
    ep = make_ep(tmp_path, title="///:*?")
    name = episode_filename(ep, topic_bucket="Animals", existing=set())
    assert name == "Animals - 10.1-x.m4a"


def test_tag_audio_sets_album_artist_for_plex_grouping(tmp_path, monkeypatch):
    # Plex Music groups albums by album-artist; without it the episode lands under
    # "Various Artists" instead of "Hugo Déziel" (confirmed against the live server).
    import mutagen

    from rabbit_hole.plex import tag_audio

    class FakeAudio(dict):
        def save(self):
            self.saved = True

    fake = FakeAudio()
    monkeypatch.setattr(mutagen, "File", lambda *a, **k: fake)

    ep = make_ep(tmp_path)
    tag_audio(tmp_path / "tmp.m4a", ep, "AI studies")

    assert fake.get("albumartist") == "Hugo Déziel"
    assert fake.get("artist") == "Hugo Déziel"
    assert fake.get("album") == "The Random Rabbit Hole"


# --- _unify_albums --------------------------------------------------------
# Plex's tv.plex.agents.music agent mints a NEW album per scanned track, so
# identical album/albumartist tags are NOT enough to keep episodes together
# (confirmed live: two tracks sharing album + albumartist + year stayed in
# separate albums even with a shared MusicBrainz Album Id). The only thing the
# agent respects is an explicit merge, so after each refresh we fold every
# same-named album back into the largest/oldest one.


class _FakeAlbum:
    def __init__(self, key, ntracks, title="The Random Rabbit Hole",
                 artist="Hugo Déziel"):
        self.ratingKey = key
        self._n = ntracks
        self.title = title
        self.parentTitle = artist

    def tracks(self):
        return list(range(self._n))


class _FakeSection:
    def __init__(self, albums):
        self._albums = albums

    def albums(self):
        return self._albums


class _FakeSession:
    def put(self):  # pragma: no cover - identity marker only
        pass


class _FakeServer:
    def __init__(self):
        self._session = _FakeSession()
        self.calls = []

    def query(self, key, method=None):
        self.calls.append((key, method))


def test_unify_albums_merges_strays_into_largest_album():
    from rabbit_hole.plex import _unify_albums

    section = _FakeSection([
        _FakeAlbum(2094, 1),
        _FakeAlbum(1933, 6),  # canonical: most tracks
        _FakeAlbum(2096, 1),
    ])
    server = _FakeServer()
    _unify_albums(section, server)

    assert len(server.calls) == 1
    key, method = server.calls[0]
    assert key == "/library/metadata/1933/merge?ids=2094,2096"
    assert method == server._session.put


def test_unify_albums_noop_when_single_matching_album():
    from rabbit_hole.plex import _unify_albums

    section = _FakeSection([
        _FakeAlbum(1933, 6),
        _FakeAlbum(5000, 3, title="Some Other Album"),
    ])
    server = _FakeServer()
    _unify_albums(section, server)

    assert server.calls == []


def test_unify_albums_ignores_other_titles_and_artists():
    from rabbit_hole.plex import _unify_albums

    section = _FakeSection([
        _FakeAlbum(1933, 6),
        _FakeAlbum(2094, 1),
        _FakeAlbum(5000, 9, title="Other Album"),       # wrong title
        _FakeAlbum(5001, 9, artist="Various Artists"),  # wrong artist
    ])
    server = _FakeServer()
    _unify_albums(section, server)

    assert len(server.calls) == 1
    key, _ = server.calls[0]
    assert key == "/library/metadata/1933/merge?ids=2094"
