"""Metadata for wave filters. Matching is pure; only prepare() touches providers or the database."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from sqlalchemy import or_, select

from app.api.schemas import GenreId, WaveFilters
from app.catalog.cache import cache_set_many, cached
from app.catalog.yandex import cis_release
from app.db import session_scope
from app.matching.normalize import has_cyrillic
from app.mixes import sources
from app.mixes.taste import artist_key
from app.models import Album, CatalogCache, Track, utcnow
from app.services import svc

if TYPE_CHECKING:
    from app.catalog.deezer import DeezerClient
    from app.mixes import build

log = logging.getLogger(__name__)
LANG_TTL = 86400
GENRE_TTL = 7 * 86400
DEEZER_GENRE_MAP_TTL = 30 * 86400
# Last.fm's common English tag names, not translated UI labels or internal IDs (e.g. hiphop).
LASTFM_GENRE_TAGS: dict[GenreId, str] = {
    "rock": "rock", "metal": "metal", "electronic": "electronic", "hiphop": "hip hop",
    "pop": "pop", "indie": "indie", "jazz": "jazz", "classical": "classical",
}
ENRICH_TIMEOUT = 2.0

# Leave a neutral band around 0.5 to avoid classifying weak projections on the uncalibrated scale.
# Revisit these cutoffs after backfilling and inspecting a real library.
ENERGY_HIGH_MIN = 0.55
ENERGY_LOW_MAX = 0.45

# Match English words (rap != trap's substring, ost != post-punk) and Russian stems.
GENRES: dict[GenreId, str] = {
    "rock": r"\b(?:rock|post punk|punk|alternative|grunge)\b|\b(?:рок|панк|альтернатив)",
    "metal": r"\b(?:metal|metalcore|deathcore|hardcore)\b|\bметал",
    "electronic": (r"\b(?:electronic[as]?|electro|house|techno|trance|dnb|drum (?:and )?bass|ambient|synthwave|"
                   r"phonk|edm|dance|idm)\b|\b(?:электрон|фонк)"),
    "hiphop": r"\b(?:hip ?hop|rap|trap|r b|rnb)\b|\b(?:хип ?хоп|рэп)",
    "pop": r"\b(?:pop|disco|estrada)\b|\b(?:поп|диско|эстрад)",
    "indie": r"\b(?:indie|shoegaze|dream pop|lo ?fi|folk|acoustic)\b|\b(?:инди|фолк)",
    "jazz": r"\b(?:jazz|blues|soul|funk)\b|\b(?:джаз|блюз|соул|фанк)",
    # Keep bare Last.fm tags, but exclude known non-classical compounds.
    "classical": (r"\b(?:classical|neoclassical|soundtrack|ost|orchestral|orchestras?|piano|"
                  r"klassik|klassisch(?:e[mnrs]?)?|symphon(?:y|ies)|symphonic (?:music|orchestras?|works?))\b|"
                  r"\b(?:baroque|chamber)\b(?! pop\b)|\b(?<!space )(?<!soap )operas?\b|"
                  r"\b(?:классик|классическ|саундтрек|оперн)|\bопера\b|"
                  r"\b(?:барочн|симфоническ|камерн)\w*\b(?! (?:поп|метал\w*|рок)\b)"),
}
_PATTERNS = {genre: re.compile(pattern) for genre, pattern in GENRES.items()}


def normalize_genres(*labels: str | None) -> set[GenreId]:
    text = " ".join(unicodedata.normalize("NFKC", label).casefold() for label in labels if label)
    text = " ".join(re.findall(r"[^\W_]+", text))
    return {genre for genre, pattern in _PATTERNS.items() if pattern.search(text)}


async def deezer_genre_ids(client: DeezerClient) -> dict[GenreId, int]:
    """Select a chart ID per filter; prefer the canonical label over related labels (e.g. Jazz over Blues)."""
    async def produce() -> dict[GenreId, int]:
        matches: dict[GenreId, tuple[bool, int]] = {}
        for row in await client.get_genres():
            if not row["id"]:  # Deezer's aggregate genre is not a match for any filter.
                continue
            name = row["name"]
            for genre in normalize_genres(name):
                exact = name.casefold() == genre
                if genre not in matches or (exact and not matches[genre][0]):
                    matches[genre] = exact, row["id"]
        return {genre: id_ for genre, (_, id_) in matches.items()}

    return await cached("mix:dz_genre_ids", DEEZER_GENRE_MAP_TTL, produce)


def _direct_ru(c: build.Cand) -> bool:
    t = c.track
    return (t.source == "hm" or any(map(has_cyrillic, (t.title, t.artist, t.album_title, *t.artists)))
            or cis_release(None, t.isrc))


def detect_lang(c: build.Cand) -> Literal["ru", "foreign"]:
    return c.wave_lang or ("ru" if _direct_ru(c) else "foreign")


def track_genres(c: build.Cand) -> set[GenreId]:
    if c.wave_genres is not None:
        return {genre for genre in GENRES if genre in c.wave_genres}
    return normalize_genres(c.track.genre)


def active(f: WaveFilters) -> bool:
    return bool(f.genres or f.lang or f.familiarity or f.energy)


def _matches_energy(c: build.Cand, mode: Literal["high", "low"], energy: Mapping[str, float | None] | None) -> bool:
    value = energy.get(c.key) if energy is not None else None
    return value is not None and (value >= ENERGY_HIGH_MIN if mode == "high" else value <= ENERGY_LOW_MAX)


def fit(c: build.Cand, f: WaveFilters, level: int, *, energy: Mapping[str, float | None] | None = None) -> bool:
    """0: strict, 1: unknown genre/familiarity, 2: any energy, 3: any genre, 4: any familiarity."""
    if f.lang and detect_lang(c) != f.lang:
        return False
    if f.energy and level < 2 and not _matches_energy(c, f.energy, energy):
        return False
    if f.genres and level < 3:
        genres = track_genres(c)
        if not genres.intersection(f.genres) and (genres or level == 0):
            return False
    if f.familiarity and level < 4:
        if c.familiar is not (f.familiarity == "known") and (c.familiar is not None or level == 0):
            return False
    return True


def matches(c: build.Cand, f: WaveFilters, *, energy: Mapping[str, float | None] | None = None) -> bool:
    return fit(c, f, 0, energy=energy)


def relaxed_axes(cands: list[build.Cand], f: WaveFilters, *, energy: Mapping[str, float | None] | None = None) -> list[str]:
    axes = []
    if f.energy and any(not _matches_energy(c, f.energy, energy) for c in cands):
        axes.append("energy")
    if f.genres and any(not track_genres(c).intersection(f.genres) for c in cands):
        axes.append("genres")
    if f.familiarity and any(c.familiar is not (f.familiarity == "known") for c in cands):
        axes.append("familiarity")
    return axes


async def _cached_values(s, keys: set[str]) -> dict:
    if not keys:
        return {}
    now = utcnow()
    rows = (await s.execute(select(CatalogCache).where(CatalogCache.key.in_(keys)))).scalars()
    return {row.key: row.payload_json.get("v") for row in rows
            if row.fetched_at + timedelta(seconds=row.ttl_s) >= now}


_RUSSIAN_ARTIST_TAG = re.compile(
    r"^(?:russian(?: speaking| language| (?:classical|rock|pop|metal|jazz|folk|indie|rap|hip hop|electronic))?"
    r"|русск(?:ий|ая|ое)(?: (?:рок|классика|поп|рэп|музыка))?|русскоязычн\w*)$"
)


def _artist_ru(tags: list[str]) -> bool:
    return any(_RUSSIAN_ARTIST_TAG.fullmatch(" ".join(re.findall(
        r"[^\W_]+", unicodedata.normalize("NFKC", tag).casefold()))) for tag in tags)


async def _prepare_lang(cands: list[build.Cand]) -> None:
    keys = {c.artist for c in cands if c.artist and not _direct_ru(c)}
    if not keys:
        for c in cands:
            c.wave_lang = "ru" if _direct_ru(c) else "foreign"
        return
    prefix = "mix:lang:"
    async with session_scope() as s:
        cached = await _cached_values(s, {prefix + key for key in keys})
        missing = {key for key in keys if prefix + key not in cached}
        if missing:
            # Narrow the batch in SQL; compare in Python using the same transliterating key as taste.
            evidence = or_(Track.artist.op("GLOB")("*[Ѐ-ӿ]*"), Track.title.op("GLOB")("*[Ѐ-ӿ]*"),
                           Track.album.op("GLOB")("*[Ѐ-ӿ]*"),
                           *(Track.isrc.ilike(code + "%") for code in ("ru", "by", "ua", "kz")))
            rows = await s.execute(select(Track.artist, Track.title, Track.album, Track.isrc).where(evidence))
            ru = {artist_key(artist) for artist, title, album, isrc in rows
                  if any(map(has_cyrillic, (artist, title, album))) or cis_release(None, isrc)}
    if missing:
        values = {prefix + key: "ru" if key in ru else "foreign" for key in missing}
        await cache_set_many(values, LANG_TTL)
        cached.update(values)
    for c in cands:
        c.wave_lang = "ru" if _direct_ru(c) or cached.get(prefix + c.artist) == "ru" else "foreign"


def _album_id(c: build.Cand) -> str | None:
    ref = c.track.album_ref or ""
    return ref[3:] if ref.startswith("dz:") and len(ref) > 3 else None


async def _prepare_metadata(cands: list[build.Cand], f: WaveFilters) -> None:
    album_ids = {aid for c in cands if f.genres and (aid := _album_id(c))}
    library_ids = {c.library_track_id for c in cands if f.genres and c.library_track_id is not None}
    genre_groups: dict[str, list[build.Cand]] = defaultdict(list)
    lang_groups: dict[str, list[build.Cand]] = defaultdict(list)
    names: dict[str, str] = {}
    use_lastfm = sources.lastfm_configured()
    prefix = "mix:artist_genre:"
    async with session_scope() as s:
        cached_albums = await _cached_values(s, {f"dz:album:{aid}" for aid in album_ids})
        library_genres = {}
        if library_ids:
            rows = await s.execute(select(Track.id, Track.genre, Album.genre).outerjoin(
                Album, Track.album_id == Album.id).where(Track.id.in_(library_ids)))
            library_genres = {tid: (track, album) for tid, track, album in rows}
        for c in cands:
            key = c.artist
            if f.lang and detect_lang(c) != "ru" and key and use_lastfm:
                lang_groups[key].append(c)
                names.setdefault(key, c.track.artist)
            elif f.lang and detect_lang(c) != f.lang:
                continue  # Direct/local Russian evidence cannot be undone by artist tags.
            if f.genres:
                album = cached_albums.get(f"dz:album:{_album_id(c)}") or {}
                labels = (*library_genres.get(c.library_track_id, ()), album.get("genre"))
                if not c.track.genre and (genre := next((x for x in labels if x), None)):
                    c.track = c.track.model_copy(update={"genre": genre})
                c.wave_genres = normalize_genres(c.track.genre, *labels)
                if key and not c.wave_genres:
                    genre_groups[key].append(c)
                    names.setdefault(key, c.track.artist)
        cached = await _cached_values(s, {prefix + key for key in genre_groups})
        tag_keys = {key: sources.lastfm_artist_tags_cache_key(names[key]) for key in names
                    if use_lastfm and (key in lang_groups or prefix + key not in cached)}
        raw_tags = await _cached_values(s, set(tag_keys.values()))
    semaphore = asyncio.Semaphore(8)
    found: dict[str, list[str]] = {}
    fetched_albums: dict[str, asyncio.Task] = {}

    async def album_genre(aid: str) -> str | None:
        if (entry := cached_albums.get(f"dz:album:{aid}")) is not None:
            return entry.get("genre")
        return (await svc.deezer.get_album(aid)).genre

    def apply_tags(key: str, tags: list[str]) -> None:
        if key in lang_groups and _artist_ru(tags):
            for c in lang_groups[key]:
                c.wave_lang = "ru"
        genre_key = prefix + key
        if key in genre_groups and genre_key not in cached and normalize_genres(*tags[:5]):
            cached[genre_key] = found[genre_key] = tags[:5]

    async def fetch_tags(key: str) -> None:
        async with semaphore:
            try:
                tags = await sources.lastfm_artist_tags(names[key], limit=20)
            except Exception as e:
                log.debug("wave: Last.fm tags for %s: %r", key, e)
            else:
                apply_tags(key, tags)  # Apply each answer without waiting for the other artists.

    async def enrich(key: str) -> None:
        if key in tag_keys and tag_keys[key] not in raw_tags:
            await fetch_tags(key)
        if key not in genre_groups or prefix + key in cached or svc.deezer is None:
            return
        aid = next((aid for c in genre_groups[key]
                    if (not f.lang or detect_lang(c) == f.lang) and (aid := _album_id(c))), None)
        if aid is None:
            return
        async with semaphore:
            if aid not in fetched_albums:
                fetched_albums[aid] = asyncio.create_task(album_genre(aid))
            try:
                genre = await fetched_albums[aid]
            except Exception as e:
                log.debug("wave: Deezer album %s: %r", aid, e)
            else:
                if normalize_genres(genre):
                    cached[prefix + key] = found[prefix + key] = [genre]

    for key, tag_key in tag_keys.items():
        if tag_key in raw_tags:
            apply_tags(key, raw_tags[tag_key] or [])
    if names:
        # Artists with a known requested genre are the best use of the language lookup budget.
        ordered = sorted(names, key=lambda key: not any(
            track_genres(c).intersection(f.genres) for c in lang_groups.get(key, ())))
        try:
            await asyncio.wait_for(asyncio.gather(*(enrich(key) for key in ordered)), ENRICH_TIMEOUT)
        except TimeoutError:
            log.debug("wave: artist enrichment reached its time budget")
        await cache_set_many(found, GENRE_TTL)
    for key, group in genre_groups.items():
        genres = normalize_genres(*(cached.get(prefix + key) or []))
        for c in group:
            c.wave_genres = genres


async def prepare(cands: list[build.Cand], f: WaveFilters) -> None:
    if not cands or not (f.lang or f.genres):
        return
    if f.lang:
        await _prepare_lang(cands)
    if f.genres or sources.lastfm_configured():
        await _prepare_metadata(cands, f)
