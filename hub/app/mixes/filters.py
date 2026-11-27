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
from app.catalog.cache import cache_set_many
from app.catalog.yandex import cis_release
from app.db import session_scope
from app.matching.normalize import has_cyrillic
from app.mixes import sources
from app.mixes.taste import artist_key
from app.models import Album, CatalogCache, Track, utcnow
from app.services import svc

if TYPE_CHECKING:
    from app.mixes import build

log = logging.getLogger(__name__)
LANG_TTL = 86400
GENRE_TTL = 7 * 86400
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


async def _prepare_lang(cands: list[build.Cand]) -> None:
    keys = {c.artist for c in cands if c.artist and not _direct_ru(c)}
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


async def _prepare_genres(cands: list[build.Cand]) -> None:
    album_ids = {aid for c in cands if (aid := _album_id(c))}
    if not cands:
        return
    library_ids = {c.library_track_id for c in cands if c.library_track_id is not None}
    async with session_scope() as s:
        cached_albums = await _cached_values(s, {f"dz:album:{aid}" for aid in album_ids})
        library_genres = {}
        if library_ids:
            rows = await s.execute(select(Track.id, Track.genre, Album.genre).outerjoin(
                Album, Track.album_id == Album.id).where(Track.id.in_(library_ids)))
            library_genres = {tid: (track, album) for tid, track, album in rows}
        groups: dict[str, list[build.Cand]] = defaultdict(list)
        for c in cands:
            album = cached_albums.get(f"dz:album:{_album_id(c)}") or {}
            labels = (*library_genres.get(c.library_track_id, ()), album.get("genre"))
            if not c.track.genre and (genre := next((x for x in labels if x), None)):
                c.track = c.track.model_copy(update={"genre": genre})
            c.wave_genres = normalize_genres(c.track.genre, *labels)
            if c.artist and not c.wave_genres:
                groups[c.artist].append(c)
        prefix = "mix:artist_genre:"
        cached = await _cached_values(s, {prefix + key for key in groups})
    if not groups:
        return
    semaphore = asyncio.Semaphore(8)
    found: dict[str, list[str]] = {}
    fetched_albums: dict[str, asyncio.Task] = {}

    async def album_genre(aid: str) -> str | None:
        if (entry := cached_albums.get(f"dz:album:{aid}")) is not None:
            return entry.get("genre")
        return (await svc.deezer.get_album(aid)).genre

    async def enrich(key: str, group: list[build.Cand]) -> None:
        async with semaphore:
            genre = None
            tags: list[str] = []
            if sources.lastfm_configured():
                try:
                    tags = await sources.lastfm_artist_tags(group[0].track.artist, limit=5)
                except Exception as e:
                    log.debug("wave: Last.fm tags for %s: %r", key, e)
            if not normalize_genres(*tags):
                # API fallback: at most one representative Deezer album per artist.
                ids = [aid for c in group if (aid := _album_id(c))]
                if svc.deezer is not None and ids:
                    aid = ids[0]
                    if aid not in fetched_albums:
                        fetched_albums[aid] = asyncio.create_task(album_genre(aid))
                    try:
                        genre = await fetched_albums[aid]
                    except Exception as e:
                        log.debug("wave: Deezer album %s: %r", aid, e)
            labels = [genre] if genre else tags
            if normalize_genres(*labels):
                cached[prefix + key] = labels
                found[prefix + key] = labels

    missing = [(key, group) for key, group in groups.items() if prefix + key not in cached]
    if missing:
        try:
            await asyncio.wait_for(asyncio.gather(*(enrich(key, group) for key, group in missing)), ENRICH_TIMEOUT)
        except TimeoutError:
            log.debug("wave: genre enrichment reached its time budget")
        await cache_set_many(found, GENRE_TTL)
    for key, group in groups.items():
        labels = cached.get(prefix + key) or []
        genres = normalize_genres(*labels)
        for c in group:
            c.wave_genres = genres


async def prepare(cands: list[build.Cand], f: WaveFilters) -> None:
    if cands and f.lang:
        await _prepare_lang(cands)
    if cands and f.genres:
        await _prepare_genres([c for c in cands if not f.lang or detect_lang(c) == f.lang])
