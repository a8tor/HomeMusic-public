"""Metadata classification and filtered wave discovery."""

from unittest.mock import AsyncMock

import pytest

from app.api.schemas import WaveFilters
from app.catalog.models import CatalogTrack, SearchResults
from app.db import session_scope
from app.matching.normalize import fingerprint
from app.mixes import wave
from app.mixes.build import Cand
from app.mixes.filters import detect_lang, fit, matches, normalize_genres, relaxed_axes, track_genres
from app.models import Album, Track
from app.services import svc
from tests.test_wave import env, fake_sources, history, tr


@pytest.mark.parametrize(("metadata", "cached", "expected"), [
    ({}, None, "foreign"),
    ({"source": "hm"}, None, "ru"),
    ({"title": "Песня"}, None, "ru"),
    ({"artist": "Кино"}, None, "ru"),
    ({"album_title": "Альбом"}, None, "ru"),
    ({"artists": ["Band", "Гость"]}, None, "ru"),
    ({"isrc": "BY1234567890"}, None, "ru"),
    ({"isrc": "UA1234567890"}, None, "ru"),
    ({"isrc": "KZ1234567890"}, None, "ru"),
    ({"isrc": "ru1234567890"}, None, "ru"),
    ({"isrc": "US1234567890"}, "ru", "ru"),
    ({"isrc": "GB1234567890"}, "foreign", "foreign"),
])
def test_detect_lang(metadata, cached, expected):
    track = CatalogTrack(**({"ref": "dz:1", "source": "dz", "id": "1", "title": "Song", "artist": "Band"}
                            | metadata))
    assert detect_lang(Cand(track, wave_lang=cached)) == expected


@pytest.mark.parametrize(("label", "expected"), [
    ("POST-PUNK / альтернативный рок", {"rock"}),
    ("Deathcore / хэви-метал", {"metal"}),
    ("Drum and Bass / фонк, электроника", {"electronic"}),
    ("Hip‑Hop / R&B / рэп", {"hiphop"}),
    ("Trap", {"hiphop"}),
    ("Estrada / диско", {"pop"}),
    ("Dream Pop / lo-fi / инди-фолк", {"indie", "pop"}),
    ("Jazz / блюз / соул", {"jazz"}),
    ("NeoClassical / OST / Саундтрек", {"classical"}),
    ("Postcard / happy / romance", set()),
    (None, set()),
])
def test_normalize_genres(label, expected):
    assert normalize_genres(label) == expected


@pytest.mark.parametrize("label", [
    "baroque",
    "opera",
    "operas",
    "klassik",
    "klassische",
    "chamber",
    "chamber music",
    "Chamber_Music",
    "symphony",
    "symphonies",
    "symphonic music",
    "orchestra",
    pytest.param("классика", id="ru-klassika"),
    pytest.param("классическая музыка", id="ru-classical"),
    pytest.param("симфоническая музыка", id="ru-symphonic"),
    pytest.param("опера", id="ru-opera"),
    pytest.param("оперная музыка", id="ru-operatic"),
    pytest.param("барочная музыка", id="ru-baroque"),
    pytest.param("камерная музыка", id="ru-chamber"),
])
def test_normalize_classical_genres(label):
    assert normalize_genres(label) == {"classical"}


@pytest.mark.parametrize(("label", "expected"), [
    ("symphonic metal", {"metal"}),
    ("symphonic rock", {"rock"}),
    ("chamber pop", {"pop"}),
    ("baroque pop", {"pop"}),
    ("space opera", set()),
    ("soap opera", set()),
    pytest.param("симфонический метал", {"metal"}, id="ru-symphonic-metal"),
])
def test_normalize_classical_genre_collisions(label, expected):
    assert normalize_genres(label) == expected


def test_track_genres_uses_prepared_genres_when_present():
    track = CatalogTrack(ref="nd:1", source="nd", id="1", title="Song", artist="Band", genre="Grunge")
    assert track_genres(Cand(track)) == {"rock"}
    assert track_genres(Cand(track, wave_genres={"rock", "indie"})) == {"rock", "indie"}


def test_a_track_in_the_uncertain_band_matches_neither():
    track = CatalogTrack(ref="nd:1", source="nd", id="1", title="Song", artist="Band")
    candidate = Cand(track)
    energy = {candidate.key: 0.5}
    for mode in ("high", "low"):
        filters = WaveFilters(energy=mode)
        assert not matches(candidate, filters, energy=energy)
        assert not fit(candidate, filters, 1, energy=energy)
        assert fit(candidate, filters, 2, energy=energy)
        assert relaxed_axes([candidate], filters, energy=energy) == ["energy"]
        assert not fit(candidate, WaveFilters(energy=mode, lang="ru"), 4, energy=energy)


def test_unknown_genre_is_not_strict():
    candidate = Cand(tr(8301))
    filters = WaveFilters(genres=["classical"])
    assert not matches(candidate, filters)
    assert fit(candidate, filters, 1)
    assert relaxed_axes([candidate], filters) == ["genres"]


@pytest.mark.parametrize(("filters", "genre", "expected_refs", "relaxed"), [
    pytest.param({"genres": ["classical"]}, "Classical", ["dz:8301"], [], id="strict-recommendation"),
    pytest.param({"genres": ["classical"]}, None, ["dz:8301"], ["genres"], id="genre-relaxed"),
    pytest.param({}, None, ["dz:8301"], [], id="empty-filters"),
    pytest.param({"genres": ["classical"], "lang": "foreign"}, None, [], ["exhausted"], id="language-never-relaxed"),
])
async def test_track_wave_genre_filter(env, monkeypatch, filters, genre, expected_refs, relaxed):
    seed = tr(8300, "Seed Artist").model_copy(update={"title": "Seed Song", "genre": "Rock"})
    monkeypatch.setattr(svc.deezer, "track", seed)
    recommendation = tr(8301).model_copy(update={"genre": genre})
    fake_sources(monkeypatch, [recommendation])
    monkeypatch.setattr(svc.deezer, "search", AsyncMock(return_value=SearchResults()))
    monkeypatch.setattr(wave, "_refill", lambda sid, rt: None)
    client = env["client"]

    response = await client.post("/api/wave/start", json={"seed": {"ref": seed.ref}, "filters": filters})
    assert response.status_code == 200, response.text
    start = response.json()
    assert start["filters"] == WaveFilters(**filters).model_dump()
    assert [t["ref"] for t in start["tracks"]][0] == seed.ref
    assert start["filter_relaxed"] == []
    await wave.settle(start["session_id"])

    response = await client.post(f'/api/wave/{start["session_id"]}/next', json={"queued": []})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert [t["ref"] for t in batch["add"]] == expected_refs
    assert batch["filter_relaxed"] == relaxed


async def test_discovery_fills_a_narrow_genre(env, monkeypatch):
    rock = tr(8400).model_copy(update={"genre": "Rock"})
    classical = [tr(8410 + i).model_copy(update={"genre": "Classical"}) for i in range(3)]
    fake_sources(monkeypatch, [rock])
    search = AsyncMock(return_value=SearchResults(tracks=[*classical, rock]))
    monkeypatch.setattr(svc.deezer, "search", search)
    client = env["client"]

    response = await client.post("/api/wave/start", json={"filters": {"genres": ["classical"]}})
    assert response.status_code == 200, response.text
    start = response.json()
    assert len(start["tracks"]) == 1 and start["tracks"][0]["ref"] in {t.ref for t in classical}
    assert start["filter_relaxed"] == []
    sid = start["session_id"]
    await wave.settle(sid)
    assert wave._rt[sid].profile.empty
    response = await client.post(f"/api/wave/{sid}/next", json={"queued": [start["tracks"][0]["ref"]]})
    assert response.status_code == 200, response.text
    assert {t["ref"] for t in response.json()["tracks"]} == {t.ref for t in classical}
    assert response.json()["filter_relaxed"] == []
    search.assert_awaited_once_with("classical", kinds=("track",), limit=wave.POOL_LOW)
    await wave.settle(sid)


async def test_discovery_respects_artist_diversity(env, monkeypatch):
    fake_sources(monkeypatch, [])
    monkeypatch.setattr(svc.deezer, "search", AsyncMock(return_value=SearchResults()))
    async with session_scope() as s:
        album = Album(uid="discovery-album", title="Shared album", album_artist="Soloist", genre="Classical")
        s.add(album)
        await s.flush()
        for i in range(13):
            artist = "Soloist" if i < 10 else f"Performer {i}"
            title = f"Piece {i}"
            s.add(Track(uid=f"discovery-{i}", nd_song_id=f"discovery-{i}", path=f"discovery-{i}.mp3",
                        artist=artist, title=title, duration_s=200,
                        album_id=album.id if i < 8 else None, album=album.title if i < 8 else None,
                        genre=None if i < 8 else "Classical", fingerprint=fingerprint(artist, title)))

    client = env["client"]
    response = await client.post("/api/wave/start", json={"filters": {"genres": ["classical"]}})
    assert response.status_code == 200, response.text
    start = response.json()
    assert len(start["tracks"]) == 1 and start["filter_relaxed"] == []
    sid = start["session_id"]
    await wave.settle(sid)
    rt = wave._rt[sid]
    candidates = [*rt.state.pool.values(), *rt.state.out.values()]
    assert 1 <= sum(c.track.artist == "Soloist" for c in candidates) <= 2
    assert sum(c.track.album_title == "Shared album" for c in candidates) <= 1
    response = await client.post(f"/api/wave/{sid}/next", json={"queued": [start["tracks"][0]["ref"]]})
    assert response.status_code == 200, response.text
    artists = [t["artist"] for t in response.json()["tracks"]]
    assert len(artists) >= 4 and artists.count("Soloist") <= 2
    assert all(a != b for a, b in zip(artists, artists[1:]))
    assert response.json()["filter_relaxed"] == []
    await wave.settle(sid)


async def test_discovery_is_off_without_filters(env, monkeypatch):
    await history(env["admin"])
    taste = tr(8500)
    fake_sources(monkeypatch, [taste])
    library = AsyncMock(return_value=[])
    search = AsyncMock(return_value=SearchResults())
    monkeypatch.setattr(wave, "_discovery_library", library)
    monkeypatch.setattr(svc.deezer, "search", search)
    client = env["client"]

    response = await client.post("/api/wave/start", json={})
    assert response.status_code == 200, response.text
    start = response.json()
    assert [t["ref"] for t in start["tracks"]] == [taste.ref]
    sid = start["session_id"]
    response = await client.post(f"/api/wave/{sid}/next", json={"queued": [taste.ref]})
    assert response.status_code == 200, response.text
    await wave.settle(sid)
    library.assert_not_awaited()
    search.assert_not_awaited()


async def test_discovery_failure_does_not_break_the_wave(env, monkeypatch, caplog):
    rock = tr(8600).model_copy(update={"genre": "Rock"})
    fake_sources(monkeypatch, [rock])
    monkeypatch.setattr(svc.deezer, "search", AsyncMock(side_effect=RuntimeError("offline")))

    response = await env["client"].post("/api/wave/start", json={"filters": {"genres": ["classical"]}})
    assert response.status_code == 200, response.text
    start = response.json()
    assert [t["ref"] for t in start["tracks"]] == [rock.ref]
    assert start["filter_relaxed"] == ["genres"]
    assert any(r.levelname == "WARNING" and "filter discovery" in r.message and "offline" in r.message
               for r in caplog.records)
    await wave.settle(start["session_id"])
