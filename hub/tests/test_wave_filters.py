"""Pure metadata classification for wave filters."""

import pytest

from app.api.schemas import WaveFilters
from app.catalog.models import CatalogTrack
from app.mixes.build import Cand
from app.mixes.filters import detect_lang, fit, matches, normalize_genres, relaxed_axes, track_genres


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
