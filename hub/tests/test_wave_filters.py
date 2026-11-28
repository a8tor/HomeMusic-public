"""Metadata classification and filtered wave discovery."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.api.schemas import WaveFilters
from app.catalog.deezer import BASE_URL, DeezerClient
from app.catalog.models import CatalogArtist, CatalogTrack, SearchResults
from app.catalog.cache import cache_set
from app.config import get_settings
from app.db import session_scope
from app.matching.normalize import fingerprint
from app.mixes import filters as wf, wave
from app.mixes.build import Cand
from app.mixes.filters import deezer_genre_ids, detect_lang, fit, matches, normalize_genres, relaxed_axes, track_genres
from app.models import Album, Track
from app.services import svc
from tests.test_wave import env as wave_env, fake_sources, history, tr


@pytest.fixture
async def env(wave_env, monkeypatch):
    monkeypatch.setattr(svc.deezer, "get_genres", AsyncMock(return_value=[]), raising=False)
    monkeypatch.setattr(svc.deezer, "genre_chart_tracks", AsyncMock(return_value=[]), raising=False)
    monkeypatch.setattr(wave.sources, "lastfm_configured", lambda: False)
    return wave_env


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


@pytest.mark.parametrize(("tag", "expected"), [
    ("russian", "ru"), ("Russian-Classical", "ru"), ("russian speaking", "ru"),
    ("not russian", "foreign"), ("russian cover", "foreign"), ("seen live", "foreign"),
])
async def test_artist_tag_infers_language_from_cached_metadata(env, monkeypatch, tag, expected):
    monkeypatch.setattr(wave.sources, "lastfm_configured", lambda: True)
    remote = AsyncMock(side_effect=AssertionError("warm cache must not call Last.fm"))
    monkeypatch.setattr(wave.sources, "lastfm_artist_tags", remote)
    artist = "P. I. Tchaikovsky"
    await cache_set(wave.sources.lastfm_artist_tags_cache_key(artist), ["classical", tag], 60)
    for lang in ("ru", "foreign"):
        candidate = Cand(tr(8310, artist=artist).model_copy(update={"title": "Swan Lake"}), familiar=False)
        filters = WaveFilters(genres=["classical"], lang=lang, familiarity="new", energy="low")
        await wf.prepare([candidate], filters)
        assert detect_lang(candidate) == expected
        assert matches(candidate, filters, energy={candidate.key: 0.35}) is (lang == expected)
    remote.assert_not_awaited()


async def test_artist_enrichment_keeps_fast_result_when_another_times_out(env, monkeypatch):
    monkeypatch.setattr(wave.sources, "lastfm_configured", lambda: True)
    monkeypatch.setattr(wf, "ENRICH_TIMEOUT", 0.2)
    calls = []
    slow_started = asyncio.Event()

    async def provider(method, *, artist):
        calls.append(artist)
        if artist == "Slow Artist":
            slow_started.set()
            await asyncio.Event().wait()
        await slow_started.wait()  # Both artists have to be fetched concurrently.
        return {"toptags": {"tag": [{"name": tag} for tag in (
            "classical", "piano", "orchestral", "chamber", "baroque", "russian")]}}

    monkeypatch.setattr(wave.sources, "_lastfm", provider)
    filters = WaveFilters(genres=["classical"], lang="ru")
    def candidate(n, artist):
        return Cand(tr(n, artist=artist).model_copy(update={"title": f"Track {n}"}))

    first = candidate(8311, "Fast Artist")
    second = candidate(8312, "Fast Artist")
    slow = candidate(8313, "Slow Artist")
    await wf.prepare([first, second, slow], filters)
    assert sorted(calls) == ["Fast Artist", "Slow Artist"]
    assert matches(first, filters) and matches(second, filters)
    assert not matches(slow, filters)
    assert await wave.sources.lastfm_artist_tags("Fast Artist", limit=1) == ["classical"]
    repeated = candidate(8314, "Fast Artist")
    await wf.prepare([repeated], filters)
    assert matches(repeated, filters)
    assert len(calls) == 2


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
    pytest.param({"genres": ["classical"]}, None, [], ["exhausted"], id="unknown-genre-excluded"),
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
    assert start["tracks"] == [] and start["pending"] is False
    assert start["filter_relaxed"] == ["exhausted"]
    assert any(r.levelname == "WARNING" and "filter discovery" in r.message and "offline" in r.message
               for r in caplog.records)
    await wave.settle(start["session_id"])


async def test_deezer_genre_mapping_is_normalized_and_cached(env, respx_mock):
    route = respx_mock.get(f"{BASE_URL}/genre").respond(json={"data": [
        {"id": 0, "name": "All"}, {"id": 11, "name": "Alternative"},
        {"id": 21, "name": "Classical", "picture": "https://picture.test/classical"},
        {"id": 22, "name": "Rock"}, {"id": 23, "name": "Pop"}, {"id": 24, "name": "Indie"},
    ]})
    chart = respx_mock.get(f"{BASE_URL}/chart/21/tracks").respond(json={"data": [
        {"id": 801, "title": "Piece", "artist": {"id": 71, "name": "Soloist"}},
    ]})
    client = DeezerClient(get_settings().cfg.catalog)
    try:
        ids = await deezer_genre_ids(client)
        assert ids == await deezer_genre_ids(client) == {"classical": 21, "rock": 22, "pop": 23, "indie": 24}
        tracks = await client.genre_chart_tracks(ids["classical"], limit=7)
        assert [(t.ref, t.artist, t.genre) for t in tracks] == [("dz:801", "Soloist", None)]
    finally:
        await client.aclose()
    assert route.call_count == 1
    assert route.calls.last.request.headers["accept-language"].startswith("en")
    assert chart.calls.last.request.url.params["limit"] == "7"


async def test_discovery_skips_missing_deezer_genre(env, monkeypatch, caplog):
    fake_sources(monkeypatch, [])
    svc.deezer.get_genres.return_value = [{"id": 23, "name": "Pop"}]
    fallback = tr(8701).model_copy(update={"genre": "Indie"})
    monkeypatch.setattr(svc.deezer, "search", AsyncMock(return_value=SearchResults(tracks=[fallback])))

    response = await env["client"].post("/api/wave/start", json={"filters": {"genres": ["indie"]}})
    assert response.status_code == 200, response.text
    start = response.json()
    assert [t["ref"] for t in start["tracks"]] == [fallback.ref]
    await wave.settle(start["session_id"])
    svc.deezer.genre_chart_tracks.assert_not_awaited()
    assert not any("filter discovery" in r.message for r in caplog.records)


@pytest.mark.parametrize("failed", [None, "chart", "lastfm"])
async def test_genre_discovery_is_strict_and_survives_source_failure(env, monkeypatch, caplog, failed):
    fake_sources(monkeypatch, [])
    filters = {"genres": ["classical"]}
    rock = tr(8800, "Not classical").model_copy(update={"genre": "Rock"})
    chart = [tr(8810 + i, "Chart soloist").model_copy(update={"genre": "Classical"}) for i in range(4)]
    tag = tr(8820, "Tag soloist").model_copy(update={"genre": "Classical"})
    fallback = tr(8830, "Text soloist").model_copy(update={"genre": "Classical"})
    svc.deezer.get_genres.return_value = [{"id": 21, "name": "Classical"}]
    svc.deezer.genre_chart_tracks.return_value = [*chart, rock] if failed != "lastfm" else [rock]
    if failed == "chart":
        svc.deezer.genre_chart_tracks.side_effect = RuntimeError("chart offline")
    monkeypatch.setattr(wave.sources, "lastfm_configured", lambda: True)
    names = AsyncMock(return_value=[tag.artist], side_effect=RuntimeError("lastfm offline") if failed == "lastfm" else None)
    monkeypatch.setattr(wave.sources, "lastfm_tag_top_artists", names)
    top = AsyncMock(return_value=[tag, rock])
    monkeypatch.setattr(svc.deezer, "get_artist_top", top, raising=False)
    search = AsyncMock(side_effect=lambda q, *, kinds, limit: SearchResults(
        artists=[CatalogArtist(ref="dz:75", source="dz", id="75", name=tag.artist)]) if kinds == ("artist",)
        else SearchResults(tracks=[fallback]))
    monkeypatch.setattr(svc.deezer, "search", search)

    response = await env["client"].post("/api/wave/start", json={"filters": filters})
    assert response.status_code == 200, response.text
    start = response.json()
    first_refs = {tag.ref} if failed == "chart" else {fallback.ref} if failed == "lastfm" else {t.ref for t in chart}
    assert len(start["tracks"]) == 1 and start["tracks"][0]["ref"] in first_refs
    assert start["filter_relaxed"] == []
    sid = start["session_id"]
    await wave.settle(sid)
    rt = wave._rt[sid]
    assert sum(c.track.artist == "Chart soloist" for c in (*rt.state.pool.values(), *rt.state.out.values())) <= 2
    response = await env["client"].post(f"/api/wave/{sid}/next", json={"queued": [start["tracks"][0]["ref"]]})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert all(t["ref"] in {tag.ref, fallback.ref, *(t.ref for t in chart)} for t in batch["tracks"])
    assert batch["filter_relaxed"] == []
    svc.deezer.genre_chart_tracks.assert_awaited_once_with(21, limit=wave.POOL_LOW)
    names.assert_awaited_once_with("classical")
    if failed != "lastfm":
        top.assert_awaited_once_with("75", limit=wave.DISCOVERY_ARTIST_TOP)
    if failed:
        assert any(r.levelname == "WARNING" and f"{failed} offline" in r.message for r in caplog.records)
    await wave.settle(sid)


async def test_lastfm_discovery_caps_unique_artists_across_genres(env, monkeypatch):
    monkeypatch.setattr(wave.sources, "lastfm_configured", lambda: True)
    genres = WaveFilters(genres=["classical", "jazz"])
    tags = AsyncMock(side_effect=lambda tag: ["Shared soloist", *(f"{tag} artist {i}" for i in range(12))])
    monkeypatch.setattr(wave.sources, "lastfm_tag_top_artists", tags)

    def search(name, *, kinds, limit):
        assert kinds == ("artist",)
        return SearchResults(artists=[
            CatalogArtist(ref="dz:71", source="dz", id="71", name="Unrelated"),
            CatalogArtist(ref="dz:72", source="dz", id="72", name=name),
        ])

    lookup = AsyncMock(side_effect=search)
    top = AsyncMock(return_value=[])
    monkeypatch.setattr(svc.deezer, "search", lookup)
    monkeypatch.setattr(svc.deezer, "get_artist_top", top, raising=False)
    assert await wave._discovery_lastfm(genres) == []
    queried = [c.args[0] for c in lookup.await_args_list]
    assert len(queried) == len(set(queried)) == wave.DISCOVERY_LASTFM_ARTISTS
    assert all(any(name.startswith(tag) for name in queried) for tag in ("classical", "jazz"))
    assert [c.args[0] for c in top.await_args_list] == ["72"] * wave.DISCOVERY_LASTFM_ARTISTS


async def test_ru_discovery_prefers_targeted_sources_and_checks_their_results(env, monkeypatch):
    fake_sources(monkeypatch, [])
    filters = {"lang": "ru", "genres": ["classical", "hiphop"]}
    classical = tr(8951, "P. I. Tchaikovsky").model_copy(update={"title": "Swan Lake"})
    foreign = tr(8952, "Foreign Soloist").model_copy(update={"title": "Sonata", "genre": "Classical"})
    rock = tr(8953, "Группа").model_copy(update={"genre": "Rock"})
    hiphop = tr(8954, "Русский автор").model_copy(update={"genre": "Hip Hop"})
    monkeypatch.setattr(wave.sources, "lastfm_configured", lambda: True)
    names = AsyncMock(side_effect=lambda tag: [classical.artist] if tag == "russian classical" else [])
    monkeypatch.setattr(wave.sources, "lastfm_tag_top_artists", names)
    monkeypatch.setattr(wave.sources, "lastfm_artist_tags", AsyncMock(side_effect=lambda name, **kw:
                        ["russian classical"] if name == classical.artist else []))
    top = AsyncMock(return_value=[classical, foreign, rock])
    monkeypatch.setattr(svc.deezer, "get_artist_top", top, raising=False)

    def search(query, *, kinds, limit):
        if kinds == ("artist",):
            assert query == classical.artist
            return SearchResults(artists=[CatalogArtist(ref="dz:75", source="dz", id="75", name=query)])
        return SearchResults(tracks=[hiphop, foreign, rock] if query == "русский рэп" else [])

    lookup = AsyncMock(side_effect=search)
    monkeypatch.setattr(svc.deezer, "search", lookup)
    svc.deezer.get_genres.return_value = [{"id": 21, "name": "Classical"}]
    svc.deezer.genre_chart_tracks.return_value = [foreign]

    client = env["client"]
    response = await client.post("/api/wave/start", json={"filters": filters})
    assert response.status_code == 200, response.text
    start = response.json()
    assert [t["ref"] for t in start["tracks"]] == [classical.ref]
    sid = start["session_id"]
    await wave.settle(sid)
    response = await client.post(f"/api/wave/{sid}/next", json={"queued": [classical.ref]})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch["keep"] == [classical.ref]
    assert [t["ref"] for t in batch["add"]] == [hiphop.ref]
    assert batch["filter_relaxed"] == []
    assert [c.args[0] for c in names.await_args_list] == ["russian classical", "russian hip hop"]
    queries = [c for c in lookup.await_args_list if c.kwargs["kinds"] == ("track",)]
    assert [c.args[0] for c in queries] == [
        "russian classical", "русская классическая музыка", "russian hip hop", "русский рэп"]
    assert sum(c.kwargs["limit"] for c in queries) == 2 * wave.POOL_LOW
    await wave.settle(sid)


async def test_discovery_continues_after_start_deadline(env, monkeypatch):
    fake_sources(monkeypatch, [])
    started, release = asyncio.Event(), asyncio.Event()
    svc.deezer.get_genres.return_value = [{"id": 21, "name": "Classical"}]
    classical = tr(8900).model_copy(update={"genre": "Classical"})

    async def slow_chart(*args, **kwargs):
        started.set()
        await release.wait()
        return []

    svc.deezer.genre_chart_tracks.side_effect = slow_chart
    search = AsyncMock(return_value=SearchResults(tracks=[classical]))
    monkeypatch.setattr(svc.deezer, "search", search)
    monkeypatch.setattr(wave, "START_FIRST_TIMEOUT_S", 0.5)
    response = await env["client"].post("/api/wave/start", json={"filters": {"genres": ["classical"]}})
    try:
        assert response.status_code == 200, response.text
        start = response.json()
        assert started.is_set() and start["pending"] and not start["tracks"]
        search.assert_not_awaited()
    finally:
        release.set()
    sid = start["session_id"]
    await wave.settle(sid)
    assert classical.ref in wave._rt[sid].state.pool
    svc.deezer.genre_chart_tracks.assert_awaited_once()
    search.assert_awaited_once()


@pytest.fixture
async def warm_wave(env, monkeypatch):
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(9000 + i) for i in range(wave.POOL_HIGH)])
    return env["client"]


async def test_pool_is_warmed_after_handout(warm_wave, monkeypatch):
    sizes = []

    def generators(profile, state):
        sizes.append(len(state.pool))

        async def radio():
            return [tr(9100 + len(sizes) * 10 + i) for i in range(wave.POOL_HIGH - sizes[-1])]

        return {"ytm_radio": radio}

    monkeypatch.setattr(wave, "refill_generators", generators)
    response = await warm_wave.post("/api/wave/start", json={})
    assert response.status_code == 200, response.text
    sid = response.json()["session_id"]
    await wave.settle(sid)
    assert sizes == [wave.POOL_HIGH - 1]
    assert len(wave._rt[sid].state.pool) == wave.POOL_HIGH

    response = await warm_wave.post(f"/api/wave/{sid}/next", json={})
    assert response.status_code == 200, response.text
    assert len(response.json()["tracks"]) == wave.BATCH
    await wave.settle(sid)
    # The old pre-handout threshold would not fire for either of these pool sizes.
    assert len(sizes) == 2 and wave.POOL_LOW <= sizes[1] < wave.POOL_HIGH
    assert len(wave._rt[sid].state.pool) == wave.POOL_HIGH


async def test_refill_is_not_scheduled_twice(monkeypatch):
    rt = wave.Runtime(1, "balanced", wave.State(), profile=wave.Profile(user_id=1, now=wave.utcnow()))
    loader = AsyncMock(return_value=[])
    rt.state.filters = WaveFilters(genres=["rock"])
    monkeypatch.setattr(wave, "_discovery_loaders", lambda filters: {"library": loader})
    monkeypatch.setattr(wave, "_save", AsyncMock())
    wave._refill("test", rt)
    task = rt.refill_task
    assert task is not None
    wave._refill("test", rt)
    assert rt.refill_task is task and rt.tasks == {task}
    await task
    loader.assert_awaited_once()


async def test_refill_respects_retry_pause(monkeypatch):
    from types import SimpleNamespace

    now = 100.0
    monkeypatch.setattr(wave, "time", SimpleNamespace(monotonic=lambda: now))
    rt = wave.Runtime(1, "balanced", wave.State(filters=WaveFilters(genres=["rock"])),
                      profile=wave.Profile(user_id=1, now=wave.utcnow()))
    loader = AsyncMock(return_value=[])
    monkeypatch.setattr(wave, "_discovery_loaders", lambda filters: {"library": loader})
    monkeypatch.setattr(wave, "_save", AsyncMock())
    wave._refill("test", rt)
    first = rt.refill_task
    await first
    now += wave.DISCOVERY_RETRY_S - 0.1
    wave._refill("test", rt)
    assert rt.refill_task is first
    loader.assert_awaited_once()
    now = rt.discovery_after["library"]
    wave._refill("test", rt)
    assert rt.refill_task is not first
    await rt.refill_task
    assert loader.await_count == 2


async def test_background_refill_does_not_block_the_answer(warm_wave, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_radio():
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(wave, "refill_generators", lambda profile, state: {"ytm_radio": slow_radio})
    sid = None
    try:
        response = await asyncio.wait_for(warm_wave.post("/api/wave/start", json={}), timeout=3)
        assert response.status_code == 200, response.text
        start = response.json()
        sid = start["session_id"]
        assert start["tracks"] and start["pending"]
        await asyncio.wait_for(entered.wait(), timeout=1)
        task = wave._rt[sid].refill_task
        response = await asyncio.wait_for(warm_wave.post(f"/api/wave/{sid}/next", json={}), timeout=3)
        assert response.status_code == 200, response.text
        assert response.json()["tracks"] and response.json()["pending"]
        assert task in wave._rt[sid].tasks and not task.done()
    finally:
        release.set()
        if sid is not None:
            await wave.settle(sid)


async def test_failing_refill_does_not_break_the_wave(warm_wave, monkeypatch, caplog):
    def broken(profile, state):
        raise RuntimeError("refill offline")

    monkeypatch.setattr(wave, "refill_generators", broken)
    response = await warm_wave.post("/api/wave/start", json={})
    assert response.status_code == 200, response.text
    sid = response.json()["session_id"]
    await wave.settle(sid)
    assert any(r.levelname == "WARNING" and "refill failed" in r.message and r.exc_info
               and str(r.exc_info[1]) == "refill offline" for r in caplog.records)
    response = await warm_wave.post(f"/api/wave/{sid}/next", json={})
    assert response.status_code == 200, response.text
    assert response.json()["tracks"]
