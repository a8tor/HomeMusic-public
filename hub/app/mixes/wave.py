"""«Моя волна» (MIXES.md 7): an endless stream for the user's taste, handed out 5 tracks at a time and picked again
after every action.

- A session (`WaveSession`) keeps a pool of candidates made by the generators of «Микс дня» (`app.mixes.build`:
  the same sources, filters and score), the tracks handed out and not started yet, the last 50 played, and the
  session's own skips and likes. Started from a track or an artist («Волна по треку») the pool comes from that
  seed's radios and the seed plays first; without one — from the taste profile, as a mix. A wave by a Navidrome
  artist (the app's «Радиостанции») adds the artist's library songs to its Deezer radios; its artist plays once in 3.
- Every `next` brings the events since the previous one and picks again: an early skip (< 30 s) or «Не нравится»
  lowers the artist and what sounds like the track (the embedder's vectors); a ♥ or a track played to its end adds
  its YouTube Music radio to the pool (`session_radio`) and raises what sounds like it. An artist plays at most once
  in 8 tracks; the mode keeps the familiar share of a mix.
- The tracks the client still has ahead (`queued`) are picked again with the rest: the answer replaces them.
- Sources answer at their own pace: a request waits for them `wave_wait_s`, what comes later joins the pool in the
  background (one lock per session). A pool running low is refilled from the radios of the next seeds.
- Tracks play as a stream from the Hub (their files are prefetched); a ♥ is an explicit import request.
- For the admin's analytics (INSIGHTS.md 6.2–6.4) a session keeps where and by what client it was started, and every
  track handed out goes to `wave_items`; a session not asked for `KEEP` has its heavy state cleared to a summary,
  the row stays. The wave itself never reads any of it.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import logging
import math
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import WaveFilters
from app.catalog.models import CatalogTrack, split_ref
from app.config import get_settings
from app.db import session_scope
from app.jobs.queue import enqueue, notify
from app.library.owned import ACTIVE_STATUSES
from app.matching.normalize import fingerprint
from app.mixes import build, embedder, filters as wf, sources
from app.mixes.resolve import catalog_track, library_track
from app.mixes.taste import EARLY_SKIP_S, Profile, TasteTrack, artist_key, build_profile, skip_weight
from app.mixes.taste_math import V2Profile, saturation
from app.mixes.taste_snapshot import TasteEvent
from app.models import Album, DownloadBatch, DownloadItem, PlayEvent, Track, User, WaveExclusion, WaveItem, WaveSession, utcnow
from app.services import svc

log = logging.getLogger(__name__)

PLAY_KINDS = ("start", "skip", "finish")

BATCH = 5
HISTORY_MAX = 50
STATE_HISTORY_MAX = 300
SAVE_DEBOUNCE_S = 1.5
PROFILE_TIMEOUT_S = 5.0
WAVE_SHOWN_TTL = timedelta(hours=12)
WAVE_SKIP_TTL = timedelta(days=3)
WAVE_NO_FILE_TTL = timedelta(seconds=86400)  # listen.KEEP_PREVIEW_S
WAVE_TRANSIENT_TTL = timedelta(minutes=5)
PICK_TOP_N = 12
PROBE_CONCURRENCY = 4
PROBE_BATCH_TIMEOUT_S = 7.0
FIRST_PROBE_TIMEOUT_S = 2.0
START_FIRST_TIMEOUT_S = 3.0
SOURCE_WAIT_S = 5.0
TASTE_TIMEOUT_S = 5.0
ARTIST_GAP = 8  # an artist at most once in this many tracks
SEED_ARTIST_GAP = 3  # ... the artist a wave started from: at most once in this many
POOL_LOW = 25  # fewer candidates left: the pool is refilled in the background
RELAXED_MAX = 300
SIGNALS_MAX = 10  # the session's latest likes / skips its sound centers are made of
SKIP_ARTIST_PENALTY = 0.6  # per early skip of the artist in this session
HANDED_TTL_S = 3600.0  # a track handed out this long ago is not offered again unless the client still has it queued
SOUND_WEIGHT = 1.0  # × (closeness to the session's likes − to its skips)
QUEUED_BONUS = 0.3  # a track the client has queued already is not swapped for a hair better one
SESSION_RADIO = "session_radio"
ARTIST_SONGS = "artist_songs"
ARTIST_SONGS_MAX = 60  # the seed artist's library songs put in the pool
ARTIST_ALBUMS_MAX = 30  # ... read from this many of their albums
SESSION_WEIGHTS = {"seed": 10.0, SESSION_RADIO: 1.2, ARTIST_SONGS: 1.2}
TITLES = {"seed": "С чего началась волна", SESSION_RADIO: "Похоже на понравившееся",
          ARTIST_SONGS: "Песни исполнителя"}
IDLE = timedelta(hours=24)  # a session not asked for this long is over
KEEP = timedelta(days=7)  # ... and this long: its state is cleared to a summary (`CLEARED_KEYS`), the row stays
CLEARED_KEYS = ("version", "taste_model", "gens", "started", "started_familiar", "skips", "seeds_used", "downloads")
EMBED_NEW = 60  # new candidates without a vector whose previews go to the embedder per refill
VECTORS_TOP = 80  # best candidates whose vectors are asked for the session's sound centers
ASK_AGAIN_S = 60  # a vector the embedder did not have is asked again after this
REFILL_SEEDS = 3


class WaveError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --- state --------------------------------------------------------------------------------------------------------


def cand_json(c: build.Cand) -> dict[str, Any]:
    return {"track": c.track.model_dump(mode="json"), "gens": c.gens, "familiar": c.familiar,
            "nd_song_id": c.nd_song_id, "library_track_id": c.library_track_id, "sim": c.sim,
            "wave_lang": c.wave_lang, "wave_genres": sorted(c.wave_genres) if c.wave_genres is not None else None}


def cand_of(d: dict[str, Any]) -> build.Cand:
    return build.Cand(track=CatalogTrack.model_validate(d["track"]), gens=dict(d.get("gens") or {}),
                      familiar=d.get("familiar", False), nd_song_id=d.get("nd_song_id"),
                      library_track_id=d.get("library_track_id"), sim=d.get("sim"),
                      wave_lang=d.get("wave_lang"),
                      wave_genres=set(d["wave_genres"]) if d.get("wave_genres") is not None else None)


def fp_of(c: build.Cand) -> str:
    return fingerprint(c.track.artist, c.track.title)


@dataclass
class Played:
    """A track of the session that started playing (the latest `HISTORY_MAX`)."""

    ref: str
    nd_song_id: str | None
    key: str  # the vector key
    artist: str  # artist key
    familiar: bool
    kind: str = "start"  # start/skip/finish (how it ended), like on top
    position_s: float | None = None
    track: dict[str, Any] | None = None  # the catalog track (the radio of a liked one, the download)


@dataclass
class State:
    version: int = 2
    taste_model: str = "v1"  # Old JSON without a version always stays v1, regardless of current config.
    seen: set[str] = field(default_factory=set)
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    positive_weights: dict[str, float] = field(default_factory=dict)
    space: dict[str, Any] | None = None
    pool: dict[str, build.Cand] = field(default_factory=dict)  # by ref: not handed out
    energy: dict[str, float | None] = field(default_factory=dict)  # key -> stored energy; None is cached unknown
    out: dict[str, build.Cand] = field(default_factory=dict)  # handed out, not started (the client's queue)
    played: list[Played] = field(default_factory=list)
    heard: set[str] = field(default_factory=set)  # fingerprints of every track started: never again
    nofile: set[str] = field(default_factory=set)  # refs no source had: a 30 s preview is no wave track
    exclusion_refs: set[str] = field(default_factory=set)
    exclusion_fps: set[str] = field(default_factory=set)
    skipped_refs: set[str] = field(default_factory=set)  # unlike `shown`, a skip also invalidates a queued track
    skipped_fps: set[str] = field(default_factory=set)
    no_file_fps: set[str] = field(default_factory=set)
    handed: dict[str, float] = field(default_factory=dict)  # ref -> when it was handed out (no repeat for a while)
    skips: dict[str, int] = field(default_factory=dict)  # artist key -> early skips in the session
    pos: list[str] = field(default_factory=list)  # vector keys of the session's likes and finished tracks
    neg: list[str] = field(default_factory=list)  # ... of its early skips and dislikes
    radios: list[str] = field(default_factory=list)  # refs whose session radio was asked
    seeds_used: list[str] = field(default_factory=list)  # taste keys the refill took radios of
    started: int = 0
    started_familiar: int = 0
    downloads: list[str] = field(default_factory=list)  # refs downloaded for the session
    gens: dict[str, Any] = field(default_factory=dict)  # generator -> {tracks, seconds, error?, off?}
    seed_artist: str | None = None  # artist key of a wave by an artist: it plays more often (`SEED_ARTIST_GAP`)
    filters: WaveFilters = field(default_factory=WaveFilters)
    pool_relaxed: dict[str, build.Cand] = field(default_factory=dict)  # in memory, like the strict pool

    def dump(self) -> dict[str, Any]:
        vectors: dict[str, list[float]] = {}
        space_meta = self.space
        if isinstance(self.space, dict):
            space_meta = {k: v for k, v in self.space.items() if k != "vectors"}
            vectors = self.space.get("vectors") if isinstance(self.space.get("vectors"), dict) else {}
        encoded = {key: base64.b64encode(array.array("f", vector).tobytes()).decode("ascii")
                   for key, vector in vectors.items() if isinstance(vector, (list, tuple))}
        return {"version": self.version, "taste_model": self.taste_model, "seen": sorted(self.seen)[-STATE_HISTORY_MAX:],
                "signals": dict(list(self.signals.items())[-STATE_HISTORY_MAX:]),
                "positive_weights": self.positive_weights, "space": space_meta,
                "space_b64": {"fmt": "f32b64", "v": encoded},
                "out": [cand_json(c) for c in self.out.values()],
                "played": [p.__dict__ for p in self.played[-STATE_HISTORY_MAX:]],
                "heard": sorted(self.heard), "handed": dict(list(self.handed.items())[-STATE_HISTORY_MAX:]),
                "nofile": sorted(self.nofile), "exclusion_refs": sorted(self.exclusion_refs),
                "skipped_refs": sorted(self.skipped_refs), "skipped_fps": sorted(self.skipped_fps),
                "exclusion_fps": sorted(self.exclusion_fps), "no_file_fps": sorted(self.no_file_fps), "skips": self.skips,
                "pos": self.pos[-STATE_HISTORY_MAX:], "neg": self.neg[-STATE_HISTORY_MAX:],
                "radios": self.radios[-STATE_HISTORY_MAX:], "seeds_used": self.seeds_used[-STATE_HISTORY_MAX:],
                "started": self.started, "started_familiar": self.started_familiar,
                "downloads": self.downloads[-STATE_HISTORY_MAX:], "gens": self.gens, "seed_artist": self.seed_artist,
                "energy": self.energy, "filters": self.filters.model_dump(mode="json")}

    @classmethod
    def load(cls, d: dict[str, Any]) -> State:
        space = d.get("space")
        packed = d.get("space_b64")
        if isinstance(packed, dict) and packed.get("fmt") == "f32b64":
            try:
                raw = packed.get("v") or {}
                vectors = {}
                for key, value in raw.items():
                    data = base64.b64decode(value, validate=True)
                    if len(data) % array.array("f").itemsize:
                        raise ValueError("invalid float32 vector size")
                    vectors[key] = list(array.array("f", data))
                if isinstance(space, dict):
                    space = {**space, "vectors": vectors}
                elif vectors:
                    space = {"vectors": vectors}
            except (ValueError, TypeError, KeyError):
                space = d.get("space")
        st = cls(version=int(d.get("version") or 1), taste_model="v2" if d.get("version") and
                 d.get("taste_model") == "v2" else "v1", seen=set(d.get("seen") or []),
                 signals=dict(d.get("signals") or {}), positive_weights=dict(d.get("positive_weights") or {}),
                 space=space, skips=dict(d.get("skips") or {}),
                 pos=list(d.get("pos") or []), neg=list(d.get("neg") or []),
                 radios=list(d.get("radios") or []), seeds_used=list(d.get("seeds_used") or []),
                 started=int(d.get("started") or 0), started_familiar=int(d.get("started_familiar") or 0),
                 downloads=list(d.get("downloads") or []), gens=dict(d.get("gens") or {}),
                 energy={k: (None if v is None else float(v)) for k, v in (d.get("energy") or {}).items()},
                 heard=set(d.get("heard") or []), seed_artist=d.get("seed_artist"),
                 nofile=set(d.get("nofile") or []), filters=WaveFilters.model_validate(d.get("filters") or {}),
                 exclusion_refs=set(d.get("exclusion_refs") or []),
                 exclusion_fps=set(d.get("exclusion_fps") or []),
                 skipped_refs=set(d.get("skipped_refs") or []), skipped_fps=set(d.get("skipped_fps") or []),
                 no_file_fps=set(d.get("no_file_fps") or []),
                 handed={k: float(v) for k, v in (d.get("handed") or {}).items()})
        st.pool = {c.track.ref: c for c in map(cand_of, d.get("pool") or [])}
        st.out = {c.track.ref: c for c in map(cand_of, d.get("out") or [])}
        st.played = [Played(**p) for p in d.get("played") or []]
        if "handed" not in d:
            # Older persisted sessions have no handout ledger; protect their current queue.
            fresh = time.time()
            st.handed = {ref: fresh for ref in st.out}
        return st

    def known_fps(self) -> set[str]:
        return self.heard | {fp_of(c) for pool in (self.pool, self.pool_relaxed, self.out) for c in pool.values()}

    def put(self, c: build.Cand) -> None:
        self.remove(c.track.ref)
        pool = self.pool if wf.matches(c, self.filters, energy=self.energy) else self.pool_relaxed
        pool[c.track.ref] = c

    def trim_reserve(self) -> None:
        if len(self.pool_relaxed) > RELAXED_MAX:
            def priority(c: build.Cand) -> tuple[int, float]:
                level = next((level for level in (1, 2, 3, 4)
                              if wf.fit(c, self.filters, level, energy=self.energy)), 5)
                return level, -c.score

            top = sorted(self.pool_relaxed.values(), key=priority)[:RELAXED_MAX]
            self.pool_relaxed = {c.track.ref: c for c in top}

    def remove(self, ref: str) -> None:
        self.pool.pop(ref, None)
        self.pool_relaxed.pop(ref, None)

    def prune_handed(self, now: float) -> None:
        """Forget the handouts of long ago: the block only has to cover a track flipped past a moment ago."""
        self.handed = {ref: at for ref, at in self.handed.items() if now - at < HANDED_TTL_S}


@dataclass
class Runtime:
    """What a session keeps in memory besides its row: the profile, the vectors asked, the lock, its tasks."""

    user_id: int
    mode: str
    state: State
    profile: Profile | None = None
    no_file_refs: set[str] = field(default_factory=set)
    probe_cooldown: dict[str, float] = field(default_factory=dict)
    salt: str = ""
    vectors: dict[str, list[float]] = field(default_factory=dict)
    asked: dict[str, float] = field(default_factory=dict)  # key -> when it was asked (monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task] = field(default_factory=set)
    refilling: bool = False
    filter_relaxed: list[str] = field(default_factory=list)  # current response, reset by each handout
    # a session of another kind (the smart shuffle, app.mixes.smart) keeps its state in its own row, and a track
    # played to its end is not downloaded for it
    save: Callable[[str, Runtime], Awaitable[None]] | None = None
    last_saved: float = 0.0
    save_pending: bool = False
    last_state_kb: int = 0
    download_finished: bool = True
    blocked: Callable[[build.Cand], bool] | None = None  # candidates the session never takes (its own songs...)


_rt: dict[str, Runtime] = {}


async def _write_exclusion(user_id: int, ref: str, fp: str | None, reason: str, ttl: timedelta) -> None:
    if not ref:
        return
    try:
        now = utcnow()
        stmt = sqlite_insert(WaveExclusion).values(user_id=user_id, ref=ref, fingerprint=fp, reason=reason,
                                                   created_at=now, expires_at=now + ttl)
        async with session_scope() as s:
            await s.execute(stmt.on_conflict_do_update(
                index_elements=["user_id", "ref", "reason"],
                set_={"fingerprint": stmt.excluded.fingerprint, "created_at": now, "expires_at": now + ttl}))
    except Exception:
        log.exception("wave exclusion write failed: user=%s reason=%s ref=%s", user_id, reason, ref)


async def _write_exclusions(user_id: int, tracks: list[build.Cand], reason: str, ttl: timedelta) -> None:
    if not tracks:
        return
    try:
        async with session_scope() as s:
            now = utcnow()
            rows = [dict(user_id=user_id, ref=c.track.ref, fingerprint=fp_of(c) or None, reason=reason,
                         created_at=now, expires_at=now + ttl) for c in tracks]
            stmt = sqlite_insert(WaveExclusion).values(rows)
            await s.execute(stmt.on_conflict_do_update(
                index_elements=["user_id", "ref", "reason"],
                set_={"fingerprint": stmt.excluded.fingerprint, "created_at": now,
                      "expires_at": stmt.excluded.expires_at}))
    except Exception:
        log.exception("wave exclusion write failed: user=%s reason=%s", user_id, reason)


async def _save(sid: str, rt: Runtime, *, force: bool = False) -> None:
    now_mono = time.monotonic()
    if not force and rt.last_saved and now_mono - rt.last_saved < SAVE_DEBOUNCE_S:
        rt.save_pending = True
        return
    started = time.monotonic()
    dumped = rt.state.dump()
    size = len(json.dumps(dumped, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    async with session_scope() as s:
        row = await s.get(WaveSession, sid)
        if row is not None:
            row.state_json, row.updated_at = dumped, utcnow()
    rt.last_saved = time.monotonic()
    rt.save_pending = False
    rt.last_state_kb = size // 1024
    log.debug("wave %s: _save=%.3fs state=%dKB (bytes=%d)", sid, time.monotonic() - started, size // 1024, size)


async def _runtime(sid: str, user: User) -> Runtime:
    rt = _rt.get(sid)
    if rt is None:
        async with session_scope() as s:
            row = await s.get(WaveSession, sid)
            if row is None or row.user_id != user.id or row.updated_at < utcnow() - IDLE:
                raise WaveError(404, "Волна закончилась: запустите новую")
            rt = Runtime(user_id=user.id, mode=row.mode, state=State.load(row.state_json or {}), salt=sid)
            if "seen" not in (row.state_json or {}):
                # Legacy state has no event ledger. Preserve its already-saved learning
                # rather than applying the old journal again when an offline client retries.
                rt.state.seen.update((await s.execute(select(PlayEvent.event_id).where(
                    PlayEvent.user_id == user.id, PlayEvent.context == f"wave:{sid}",
                    PlayEvent.received_at <= row.updated_at))).scalars())
        rt = _rt.setdefault(sid, rt)
    elif rt.user_id != user.id:
        raise WaveError(404, "Волна закончилась: запустите новую")
    return rt


async def _profile(rt: Runtime, user: User, *, timings: dict[str, float] | None = None) -> Profile:
    started = time.monotonic()
    try:
        async with session_scope() as s:
            rt.profile = await asyncio.wait_for(
                build_profile(s, user, model=rt.state.taste_model), PROFILE_TIMEOUT_S)
    except TimeoutError:
        log.warning("wave: build_profile timed out after %.1fs; using cached/minimal profile", PROFILE_TIMEOUT_S)
        if rt.profile is None:
            rt.profile = Profile(user_id=user.id, now=utcnow())
    finally:
        if timings is not None:
            timings["_profile"] = time.monotonic() - started
    if isinstance(rt.profile, V2Profile):
        from app.mixes.taste_select import prepare
        try:
            await asyncio.wait_for(prepare(rt.profile), TASTE_TIMEOUT_S)
        except TimeoutError:
            log.warning("wave: taste preparation timed out; continuing without taste geometry")
    return rt.profile


# --- generators ---------------------------------------------------------------------------------------------------


def taste_of(t: CatalogTrack) -> TasteTrack:
    return TasteTrack(key=t.ref, ref=t.ref, artist=t.artist, title=t.title, duration_s=t.duration_s, track=t)


async def artist_radio(artist_ref: str) -> list[CatalogTrack]:
    if svc.deezer is None:
        raise build.Off("Deezer")
    return await svc.deezer.get_artist_radio(split_ref(artist_ref)[1])


async def radio_of(t: CatalogTrack) -> list[CatalogTrack]:
    """YouTube Music's radio of a track the user liked or played to its end in this session."""
    return await build._by_seeds([taste_of(t)], lambda x: sources.ytm_radio(x.artist, x.title), "ytm")


async def _prioritize_seeds(p: Profile, seeds: list[TasteTrack], filters: WaveFilters) -> list[TasteTrack]:
    if not seeds or not wf.active(filters):
        return seeds
    cands = [build.Cand(track=t.track or CatalogTrack(ref=t.ref or t.key, source="nd", id=t.key,
                                                    title=t.title, artist=t.artist), nd_song_id=t.nd_song_id)
             for t in seeds]
    cfg = get_settings().cfg.mixes
    for c in cands:
        build.keep(c, p, cfg)
    await wf.prepare(cands, filters)
    # The energy of a radio seed does not describe the tracks its radio returns.
    metadata_filters = filters.model_copy(update={"energy": None})
    return [t for t, _ in sorted(zip(seeds, cands, strict=True), key=lambda x: not wf.matches(x[1], metadata_filters))]


def generators(p: Profile, seed: CatalogTrack | None, seed_artist: dict[str, Any] | None,
               seeds: list[TasteTrack], *, filters: WaveFilters | None = None) -> dict[str, build.Gen]:
    """The sources of a new session's pool: the seed's radios, or the mix's generators for the profile."""
    if seed is not None:
        gens = build.generators(p, [taste_of(seed)])
        out = {k: gens[k] for k in ("ytm_radio", "lastfm_similar", "yandex_similar")}
        if seed.artist_ref and seed.artist_ref.startswith("dz:"):
            out["deezer_radio"] = lambda: artist_radio(seed.artist_ref or "")
        return out
    if seed_artist is not None:
        ref = seed_artist.get("ref")
        songs: list[CatalogTrack] = list(seed_artist.get("songs") or [])

        async def top() -> list[CatalogTrack]:
            if svc.deezer is None:
                raise build.Off("Deezer")
            return await svc.deezer.get_artist_top(split_ref(ref)[1], limit=10)

        async def own() -> list[CatalogTrack]:
            return random.sample(songs, len(songs))

        async def radios() -> list[CatalogTrack]:
            if filters is not None and wf.active(filters):
                tracks = (await top()) if ref else random.sample(songs, len(songs))
                selected = (await _prioritize_seeds(p, [taste_of(t) for t in tracks], filters))[:2]
            else:
                tracks = (await top())[:2] if ref else random.sample(songs, min(2, len(songs)))
                selected = [taste_of(t) for t in tracks]
            return await build._by_seeds(selected, lambda x: sources.ytm_radio(x.artist, x.title), "ytm")

        out = {"deezer_radio": lambda: artist_radio(ref), "favorites": top} if ref else {}
        if songs:
            out[ARTIST_SONGS] = own
        return {**out, "ytm_radio": radios}
    return build.generators(p, seeds)


def _refill_seeds(p: Profile, st: State) -> list[TasteTrack]:
    cfg = get_settings().cfg.mixes
    pool = [t for t in p.seeds(cfg.seeds * 4) if t.key not in st.seeds_used]
    if not pool and not wf.active(st.filters):
        st.seeds_used.clear()
        pool = p.seeds(cfg.seeds * 4)
    return pool


def _refill_gens(p: Profile, st: State, seeds: list[TasteTrack]) -> dict[str, build.Gen]:
    st.seeds_used.extend(t.key for t in seeds)
    if not seeds:
        return {}
    gens = build.generators(p, seeds)
    return {k: gens[k] for k in ("ytm_radio", "lastfm_similar")}


def refill_generators(p: Profile, st: State, *, prioritized: list[TasteTrack] | None = None) -> dict[str, build.Gen]:
    """Radios of the next unused seeds, already ranked by filters when active."""
    seeds = prioritized if prioritized is not None else _refill_seeds(p, st)
    return _refill_gens(p, st, seeds[:REFILL_SEEDS])


# --- the pool -----------------------------------------------------------------------------------------------------


def weights() -> dict[str, float]:
    return {**get_settings().cfg.mixes.weights, **SESSION_WEIGHTS}


async def _fetch_energy(st: State, candidates: list[build.Cand]) -> None:
    if not st.filters.energy or not embedder.configured():
        return
    keys = list(dict.fromkeys(c.key for c in candidates if c.key not in st.energy))
    if not keys:
        return
    try:
        async with embedder.EmbedderClient() as client:
            scores = await client.energies(keys)
    except Exception as e:
        log.warning("wave: energy lookup failed: %s", e)
        return
    st.energy.update({key: scores[key] for key in keys if key in scores})


async def absorb(rt: Runtime, results: list[build.GenResult]) -> int:
    """New candidates into the pool: one per song (a song already there adds the generator), library songs marked,
    the mix's filters, its score with the closeness to the taste by sound. Returns how many were added."""
    p = rt.profile
    assert p is not None
    st = rt.state
    for r in results:
        g = st.gens.setdefault(r.name, {"tracks": 0, "seconds": 0.0})
        g["tracks"] += len(r.tracks)
        g["seconds"] = round(g["seconds"] + r.seconds, 2)
        if r.error:
            g["error"], g["off"] = r.error, r.off
    merged = build.merge(results)
    by_fp = {fp_of(c): c for pool in (st.pool, st.pool_relaxed) for c in pool.values()}
    known = st.known_fps()
    fresh: dict[str, build.Cand] = {}
    for c in merged.values():
        fp = fp_of(c)
        if (have := by_fp.get(fp)) is not None:
            for g, pos in c.gens.items():
                have.gens[g] = min(have.gens.get(g, pos), pos)
            have.score = build.score(have, weights(), p)
        elif fp not in known:
            fresh[c.track.ref] = c
    if not fresh:
        return 0
    await build.link_library(fresh)
    cfg = get_settings().cfg.mixes
    kept = [c for c in fresh.values() if not (rt.blocked and rt.blocked(c)) and build.keep(c, p, cfg)]
    await _fetch_energy(st, kept)
    await wf.prepare(kept, st.filters)
    if not isinstance(p, V2Profile):
        try:
            await asyncio.wait_for(_sound(p, kept), TASTE_TIMEOUT_S)
        except TimeoutError:
            log.warning("wave: candidate taste scoring timed out; continuing without sound weights")
    for c in kept:
        c.score = build.score(c, weights(), p)
        st.put(c)
    st.trim_reserve()
    return len(kept)


async def _sound(p: Profile, cands: list[build.Cand]) -> None:
    """`sim` of new candidates (the embedder); the previews of the best ones without a vector are embedded for the
    picks to come (nothing waits for them). The service down — no audio term."""
    if not cands or not embedder.configured():
        return
    try:
        async with embedder.EmbedderClient() as e:
            sim = await e.similarity([c.key for c in cands], p.vector_profile())
            for c in cands:
                c.sim = sim.scores.get(c.key)
            todo = sorted((c for c in cands if c.key in sim.missing and c.track.source == "dz"),
                          key=lambda c: -build.score(c, weights(), p))[:EMBED_NEW]
            urls = await asyncio.gather(*(build._preview(c.track) for c in todo))
            items = [{"key": c.key, "url": u} for c, u in zip(todo, urls, strict=True) if u]
            if items:
                await e.embed(items)
    except embedder.EmbedderDown as e:
        log.info("wave: embedder unavailable: %s", e)


async def _gather(sid: str, rt: Runtime, gens: list[tuple[str, build.Gen]], enough: Callable[[], bool]) -> None:
    """Runs the generators (a name may repeat: the session's radios); waits for them `wave_wait_s` (or until
    `enough`), absorbing each as it answers. Those still running then join the pool in the background. The caller
    holds the session's lock."""
    if not gens:
        return
    tasks = {asyncio.create_task(build.run_generators({n: f})) for n, f in gens}
    deadline = time.monotonic() + min(get_settings().wave_wait_s, SOURCE_WAIT_S)
    pending = tasks
    while pending:
        done, pending = await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()),
                                           return_when=asyncio.FIRST_COMPLETED)
        if not done:
            break
        await absorb(rt, [r for t in done for r in t.result()])
        if enough():
            break
    for t in pending:
        late = asyncio.create_task(_late(sid, rt, t))
        rt.tasks.add(late)
        late.add_done_callback(rt.tasks.discard)


async def _late(sid: str, rt: Runtime, task: asyncio.Task) -> None:
    try:
        results = await task
        async with rt.lock:
            added = await absorb(rt, results)
            await (rt.save or _save)(sid, rt)
        log.info("wave %s: %s joined later (+%s)", sid, [r.name for r in results], added)
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception:
        log.exception("wave %s: a late source failed", sid)


def _refill(sid: str, rt: Runtime) -> None:
    """The pool runs low: the radios of the next seeds, in the background."""
    if rt.refilling or rt.profile is None:
        return
    v2 = isinstance(rt.profile, V2Profile)
    profile = rt.profile
    chosen = []
    filtered = wf.active(rt.state.filters)
    seed_pool = _refill_seeds(profile, rt.state) if filtered else []
    if filtered and not seed_pool:
        return
    if v2 and not filtered:
        pool = [t for t in profile.seeds(get_settings().cfg.mixes.seeds * 4) if t.key not in rt.state.seeds_used]
        if not pool:
            rt.state.seeds_used.clear()
            pool = profile.seeds(get_settings().cfg.mixes.seeds * 4)
        chosen = pool[:REFILL_SEEDS]
        rt.state.seeds_used.extend(t.key for t in chosen)
    gens = {} if v2 or filtered else refill_generators(rt.profile, rt.state)
    if not gens and not (v2 or filtered):
        return
    rt.refilling = True

    async def run() -> None:
        try:
            current = gens
            if filtered:
                ordered = await _prioritize_seeds(profile, seed_pool, rt.state.filters)
                chosen_now = ordered[:REFILL_SEEDS]
                if not v2:
                    current = refill_generators(profile, rt.state, prioritized=chosen_now)
                else:
                    rt.state.seeds_used.extend(t.key for t in chosen_now)
            else:
                chosen_now = chosen
            if v2:
                from app.mixes.taste_select import enrich_selected
                chosen_now = await enrich_selected(profile, chosen_now)
            if v2:
                all_gens = build.generators(profile, chosen_now) if chosen_now else {}
                current = {k: all_gens[k] for k in ("ytm_radio", "lastfm_similar") if k in all_gens}
            results = await build.run_generators(current)
            async with rt.lock:
                await absorb(rt, results)
                await (rt.save or _save)(sid, rt)
        except Exception:
            log.exception("wave %s: refill failed", sid)
        finally:
            rt.refilling = False

    t = asyncio.create_task(run())
    rt.tasks.add(t)
    t.add_done_callback(rt.tasks.discard)


# --- picking --------------------------------------------------------------------------------------------------------


def _center(keys: list[str], vectors: dict[str, list[float]]) -> list[float] | None:
    vs = [vectors[k] for k in keys if vectors.get(k)]
    if not vs:
        return None
    c = [sum(x) / len(vs) for x in zip(*vs, strict=False)]
    n = math.sqrt(sum(x * x for x in c)) or 1.0
    return [x / n for x in c]


def _weighted_center(weights: dict[str, float], vectors: dict[str, list[float]]) -> list[float] | None:
    rows = [(vectors[k], weight) for k, weight in weights.items() if k in vectors and weight > 0]
    if not rows:
        return None
    center = [sum(v[i] * weight for v, weight in rows) for i in range(len(rows[0][0]))]
    norm = math.sqrt(sum(x * x for x in center))
    return [x / norm for x in center] if norm > 1e-12 else None


def _session_weights(st: State, profile: V2Profile) -> None:
    from app.favorites import fold
    from app.mixes.taste_snapshot import Subject

    events, by_id, positives, negative, skips = [], {}, {}, [], {}
    for eid, data in sorted(st.signals.items(), key=lambda x: (x[1]["ts"], x[0])):
        values = {k: data[k] for k in TasteEvent.__dataclass_fields__ if k in data}
        values["ts"] = datetime.fromisoformat(values["ts"])
        values["received_at"] = datetime.fromisoformat(values["received_at"])
        e = TasteEvent(**values)
        t = profile.find(ref=e.ref, nd_song_id=e.nd_song_id)
        key = t.key if t else data["key"]
        events.append(e)
        by_id[eid] = key, data["artist"]
        if e.reverts_event_id:
            continue
        if e.kind == "finish":
            positives[key] = positives.get(key, 0) + 2
        elif e.kind == "skip" and e.position_s is not None:
            if e.position_s < EARLY_SKIP_S:
                negative.append(key)
                skips[data["artist"]] = skips.get(data["artist"], 0) + 1
            else:
                positives[key] = positives.get(key, 0) + skip_weight(e.position_s, e.duration_s or data.get("length"))
    subjects = [Subject(x.target_type, tuple(x.aliases), x.ref, x.nd_id, x.data or {})
                for x in profile.favorite_states.values()]
    for state in fold(events, subjects).values():
        if not state.liked:
            continue
        key, _ = by_id[state.event.event_id]
        current = profile.find(ref=state.ref, nd_song_id=state.nd_id)
        # A fresh global unlike removes only the session like, never finish/late-skip evidence.
        journalled = any(x.target_type == "track" and
                         ((state.ref and x.ref == state.ref) or (state.nd_id and x.nd_id == state.nd_id))
                         for x in profile.favorite_states.values())
        if not journalled or (current and current.liked):
            positives[key] = positives.get(key, 0) + 1
    explicit = {}
    for e in events:
        if e.kind in ("dislike", "undislike"):
            explicit[by_id[e.event_id][0]] = e
    for key, e in explicit.items():
        if e.kind == "dislike":
            negative.append(key)
            artist = by_id[e.event_id][1]
            skips[artist] = skips.get(artist, 0) + 1
    st.positive_weights = {key: saturation(value) for key, value in positives.items() if value > 0}
    st.pos = list(st.positive_weights)[-SIGNALS_MAX:]
    st.positive_weights = {key: st.positive_weights[key] for key in st.pos}
    st.neg = list(dict.fromkeys(reversed(negative)))[:SIGNALS_MAX]
    st.skips = skips


async def _learn_v2(sid: str, rt: Runtime, events: list[PlayEvent]) -> None:
    st, liked = rt.state, []
    assert isinstance(rt.profile, V2Profile)
    eligible_events = [e for e in events if e.user_id == rt.user_id and e.ts <= utcnow()
                       and not (e.client or "").lower().endswith("-debug")]
    await _record_early_skips(rt, eligible_events)
    for e in events:
        if e.user_id != rt.user_id or not e.event_id or e.event_id in st.seen:
            continue
        # Future/delayed events remain unconsumed until their occurrence time arrives.
        if e.ts > utcnow() or (e.client or "").lower().endswith("-debug"):
            continue
        st.seen.add(e.event_id)
        if (e.target_type or "track") != "track":
            continue
        matched = _match(st, e.ref, e.nd_song_id)
        if matched is None:
            old = next((d for d in reversed(list(st.signals.values())) if
                        (e.ref and d.get("ref") == e.ref) or
                        (e.nd_song_id and d.get("nd_song_id") == e.nd_song_id)), None)
            if old is None:
                continue
            matched = "signal", Played(old["ref"], old.get("nd_song_id"), old["key"], old["artist"],
                                        bool(old.get("familiar")), track=old.get("track"))
        x = matched[1]
        if isinstance(x, build.Cand):
            x = _start(st, x) if e.kind in PLAY_KINDS else _as_played(x)
        if e.kind in ("skip", "finish"):
            x.kind, x.position_s = e.kind, e.position_s
        values = {k: getattr(e, k) for k in TasteEvent.__dataclass_fields__}
        values["ts"], values["received_at"] = e.ts.isoformat(), e.received_at.isoformat()
        st.signals[e.event_id] = {**values, "key": x.key, "artist": x.artist,
                                  "length": (x.track or {}).get("duration_s"), "track": x.track,
                                  "familiar": x.familiar}
        if e.kind in ("finish", "like") and not e.reverts_event_id:
            liked.append(x)
            if e.kind == "finish":
                await _download(sid, rt, x)
    _session_weights(st, rt.profile)
    radios = []
    for x in liked:
        if x.ref not in st.radios and x.track:
            st.radios.append(x.ref)
            track = CatalogTrack.model_validate(x.track)
            radios.append((SESSION_RADIO, lambda track=track: radio_of(track)))
    await _gather(sid, rt, radios, lambda: False)


def value(c: build.Cand, st: State, vectors: dict[str, list[float]], pos: list[float] | None,
          neg: list[float] | None, queued: bool = False) -> float:
    """The mix's score, minus the session's early skips of the artist, plus closeness by sound to what the session
    liked minus to what it skipped."""
    v = c.score - SKIP_ARTIST_PENALTY * st.skips.get(c.artist, 0) + (QUEUED_BONUS if queued else 0.0)
    vec = vectors.get(c.key)
    if vec is not None:
        if st.taste_model == "v2":
            nearest = max((max(0.0, build._cos(vec, vectors.get(key))) for key in st.neg), default=0.0)
            v += SOUND_WEIGHT * (build._cos(vec, pos) - .5 * nearest)
        else:
            v += SOUND_WEIGHT * (build._cos(vec, pos) - build._cos(vec, neg))
    return v


def _guaranteed_full(c: build.Cand) -> bool:
    return bool(c.nd_song_id or c.library_track_id or c.track.source in ("nd", "file"))


def _known_preview(st: State, c: build.Cand) -> bool:
    return c.track.ref in st.nofile or fp_of(c) in st.no_file_fps


def pick(st: State, p: Profile, mode: str, vectors: dict[str, list[float]], n: int = BATCH,
         queued: set[str] = frozenset(), first: build.Cand | None = None,
         values: dict[str, float] | None = None, *, prefix: tuple[build.Cand, ...] = (),
         filter_level: int = 0, eligible: Callable[[build.Cand], bool] | None = None) -> list[build.Cand]:
    """The next `n`: the best by `value` keeping the mode's familiar share, an artist at most once in `ARTIST_GAP`
    tracks (counting the played ones; the artist of a wave by an artist — once in `SEED_ARTIST_GAP`); with nothing else left, the gap gives way (never the same artist twice in a
    row). «Не нравится» set since the candidate came in drops it. `queued` — refs the client has ahead already.
    `prefix` is already selected and counts for deduplication, spacing and familiar share, but is not returned.
    `values` gets the `value` of every candidate ranked, by ref (the analytics of `wave_items`)."""
    pos = _weighted_center(st.positive_weights, vectors) if st.taste_model == "v2" else _center(st.pos, vectors)
    neg = None if st.taste_model == "v2" else _center(st.neg, vectors)
    cfg = get_settings().cfg.mixes if isinstance(p, V2Profile) else None
    if first is not None and (_known_preview(st, first)
                              or (eligible is not None and not eligible(first))
                              or not wf.fit(first, st.filters, filter_level, energy=st.energy)):
        first = None
    pools = (st.out, st.pool, st.pool_relaxed) if filter_level or st.filters.familiarity else (st.out, st.pool)
    candidates = [c for pool in pools for c in pool.values()
                  if c is not first and (eligible is None or eligible(c)) and not _known_preview(st, c)
                  and not _disliked(c, p)
                  and (cfg is None or build.keep(c, p, cfg)) and wf.fit(c, st.filters, filter_level, energy=st.energy)]
    # Catalog and library copies can have different refs for the same song. Never put both into one wave batch,
    # re-offer a song that has already started, or substitute another ref for one still queued by the client.
    seen = st.heard | {fp_of(c) for c in prefix}
    seen.update(fp_of(c) for ref, c in st.out.items() if ref in queued)
    if first is not None:
        seen.add(fp_of(first))
    # A track flipped past before it started is not in `heard`: it comes back to the pool as if it were new. Keep
    # everything handed out recently out of the batch — the client still having it queued is what keeps it alive.
    # Newly selected tracks still observe the handout block; the delta keeps valid queued tracks separately.
    now = time.time()  # wall clock: `handed` outlives the process (it is in the session's JSON)
    fresh = {fp_of(c) for pool in (st.pool, st.pool_relaxed, st.out) for ref, c in pool.items()
             if now - st.handed.get(ref, -HANDED_TTL_S) < HANDED_TTL_S
             and (not queued or ref not in queued)}
    pool: list[build.Cand] = []
    excluded_shown: list[build.Cand] = []
    for c in candidates:
        fp = fp_of(c)
        if not fp or fp in seen or fp in fresh:
            continue
        if c.track.ref in st.nofile or fp in st.no_file_fps or _skipped(st, c, fp):
            continue
        if c.track.ref in st.exclusion_refs or fp in st.exclusion_fps:
            if c.track.ref in queued:
                seen.add(fp)
                pool.append(c)
            else:
                excluded_shown.append(c)
            continue
        seen.add(fp)
        pool.append(c)
    if len(pool) < n:
        for c in excluded_shown:
            fp = fp_of(c)
            if fp not in seen:
                seen.add(fp)
                pool.append(c)
    vals = {id(c): value(c, st, vectors, pos, neg, c.track.ref in queued) for c in pool}
    if values is not None:
        values.update((c.track.ref, vals[id(c)]) for c in pool)
    ranked = sorted(pool, key=lambda c: -vals[id(c)])
    share = build.MODES.get(mode, build.MODES["normal"])
    out: list[build.Cand] = [first] if first is not None else []
    recent = [x.artist for x in st.played] + [c.artist for c in prefix]
    while len(out) < n and ranked:
        seq = recent + [c.artist for c in out]
        gap = set(seq[-(ARTIST_GAP - 1):])
        seed_gap = set(seq[-(SEED_ARTIST_GAP - 1):])
        ok = [c for c in ranked if c.artist not in (seed_gap if c.artist == st.seed_artist else gap)]
        choices = ok
        if st.filters.familiarity is None:
            total = st.started + len(prefix) + len(out)
            fam = st.started_familiar + sum(bool(c.familiar) for c in (*prefix, *out))
            want_fam = fam + 1 <= share * (total + 1) + 0.5
            choices = [c for c in ok if c.familiar == want_fam] or ok
        window = choices[:PICK_TOP_N]
        if window:
            top = vals[id(window[0])]
            near = [c for c in window if top - vals[id(c)] <= max(0.25, abs(top) * 0.05)]
            rng = random.Random(f"{getattr(st, 'salt', '')}:{len(out)}")
            best = rng.choice(near)
        else:
            best = next((c for c in ranked if not seq or c.artist != seq[-1]), None)
        if best is None:
            break
        ranked.remove(best)
        out.append(best)
    return out


def _disliked(c: build.Cand, p: Profile) -> bool:
    t = c.track
    return p.disliked(ref=t.ref if t.source in ("dz", "file") else None, nd_song_id=c.nd_song_id, artist=t.artist,
                      artist_ref=t.artist_ref)


async def _vectors(rt: Runtime) -> dict[str, list[float]]:
    """Vectors of the best candidates and of the session's likes and skips (asked once, missing ones again a minute
    later)."""
    st = rt.state
    if st.taste_model == "v2":
        from app.mixes.taste_select import geometry_scores
        p = rt.profile
        assert isinstance(p, V2Profile)
        candidates = [*st.out.values(), *st.pool.values()]
        rt.vectors = await geometry_scores(p, candidates, [*st.pos, *st.neg])
        st.space = {"model": p.geometry.model, "space_id": p.geometry.space_id,
                    "dim": p.geometry.dim} if p.geometry else None
        for c in candidates:
            c.score = build.score(c, weights(), p)
        if p.geometry:
            todo = sorted((c for c in candidates if c.sim is None and c.track.source == "dz"),
                          key=lambda c: -c.score)[:EMBED_NEW]
            urls = await asyncio.gather(*(build._preview(c.track) for c in todo))
            items = [{"key": c.key, "url": url} for c, url in zip(todo, urls, strict=True) if url]
            if items:
                try:
                    async with embedder.EmbedderClient() as client:
                        await client.embed(items)
                except embedder.EmbedderDown:
                    pass
        return rt.vectors
    if not embedder.configured() or not (st.pos or st.neg):
        return rt.vectors
    best = sorted([*st.out.values(), *st.pool.values()], key=lambda c: -c.score)[:VECTORS_TOP]
    now = time.monotonic()
    keys = [k for k in dict.fromkeys([*st.pos, *st.neg, *(c.key for c in best)])
            if k not in rt.vectors and rt.asked.get(k, 0) < now - ASK_AGAIN_S]
    if keys:
        try:
            async with embedder.EmbedderClient() as e:
                rt.vectors.update(await e.vectors(keys))
        except embedder.EmbedderDown as e:
            log.info("wave: no vectors: %s", e)
        for k in keys:
            rt.asked[k] = now
    return rt.vectors


# --- the session's events ----------------------------------------------------------------------------------------


def _match(st: State, ref: str | None, nd: str | None) -> tuple[str, build.Cand | Played] | None:
    """The handed out track an event names (by its ref or Navidrome song; `nd:<song>` refs too)."""
    if c := _match_out(st, ref, nd):
        return "out", c
    if ref and ref.startswith("nd:"):
        nd = nd or ref[3:]
    for x in reversed(st.played):
        if (ref and x.ref == ref) or (nd and x.nd_song_id == nd):
            return "played", x
    return None


def _match_out(st: State, ref: str | None, nd: str | None = None) -> build.Cand | None:
    if ref is not None and ref in st.out:
        return st.out[ref]
    if ref:
        nd = nd or (ref[3:] if ref.startswith("nd:") else ref)
    alias = (st.pool.get(ref) or st.pool_relaxed.get(ref)) if ref else None
    fp = fp_of(alias) if alias else ref  # a known alternative ref, or the fingerprint itself
    return next((c for c in st.out.values() if (nd and c.nd_song_id == nd) or (fp and fp_of(c) == fp)), None)


def _skipped(st: State, c: build.Cand, fp: str) -> bool:
    return (c.track.ref in st.skipped_refs or fp in st.skipped_fps
            or (c.nd_song_id is not None and f"nd:{c.nd_song_id}" in st.skipped_refs))


def _remember_skip(st: State, ref: str, fp: str | None) -> None:
    if ref:
        st.skipped_refs.add(ref)
        st.exclusion_refs.add(ref)
    if fp:
        st.skipped_fps.add(fp)
        st.exclusion_fps.add(fp)


def _start(st: State, c: build.Cand) -> Played:
    st.out.pop(c.track.ref, None)
    st.remove(c.track.ref)
    st.handed.pop(c.track.ref, None)  # it is in `heard` now: the handout block is no longer needed
    x = _as_played(c)
    st.played = [*st.played, x][-HISTORY_MAX:]
    st.heard.add(fp_of(c))
    st.started += 1
    st.started_familiar += bool(c.familiar)
    return x


def _signal(keys: list[str], key: str) -> None:
    if key in keys:
        keys.remove(key)
    keys.append(key)
    del keys[:-SIGNALS_MAX]


def _as_played(c: build.Cand) -> Played:
    return Played(ref=c.track.ref, nd_song_id=c.nd_song_id, key=c.key, artist=c.artist, familiar=bool(c.familiar),
                  track=c.track.model_dump(mode="json"))


async def _record_early_skips(rt: Runtime, events: list[PlayEvent]) -> None:
    for e in events:
        if ((e.target_type or "track") != "track" or e.kind != "skip" or e.reverts_event_id
                or (e.position_s or 0) >= EARLY_SKIP_S):
            continue
        track = rt.profile.find(ref=e.ref, nd_song_id=e.nd_song_id) if rt.profile is not None else None
        matched = _match_out(rt.state, e.ref, e.nd_song_id)
        fp = fp_of(matched) if matched is not None else track.fp if track is not None else None
        ref = e.ref or (f"nd:{e.nd_song_id}" if e.nd_song_id else "")
        await _write_exclusion(rt.user_id, ref, fp, "skipped", WAVE_SKIP_TTL)
        _remember_skip(rt.state, ref, fp)


async def learn(sid: str, rt: Runtime, events: list[PlayEvent]) -> None:
    """What the session's events say: starts, early skips, likes, tracks played to the end."""
    if rt.state.taste_model == "v2":
        await _learn_v2(sid, rt, events)
        return
    st = rt.state
    liked: list[Played] = []
    for e in events:
        if e.event_id and e.event_id in st.seen:
            continue
        if e.event_id:
            st.seen.add(e.event_id)
        if (e.target_type or "track") != "track" or e.reverts_event_id:
            continue  # Typed subjects and causal rollbacks are journal data, not a track playback signal.
        m = _match(st, e.ref, e.nd_song_id)
        if m is None:
            continue
        x = m[1]
        if isinstance(x, build.Cand):  # handed out: it starts now (a ♥ of a track ahead doesn't start it)
            x = _start(st, x) if e.kind in PLAY_KINDS else _as_played(x)
        if e.kind in ("skip", "finish"):
            x.kind, x.position_s = e.kind, e.position_s
        if e.kind == "skip" and (e.position_s or 0) < EARLY_SKIP_S:
            fp = fingerprint(x.track.get("artist", ""), x.track.get("title", "")) if x.track else None
            await _write_exclusion(rt.user_id, x.ref, fp, "skipped", WAVE_SKIP_TTL)
            _remember_skip(st, x.ref, fp)
        if (e.kind == "skip" and (e.position_s or 0) < EARLY_SKIP_S) or e.kind == "dislike":
            st.skips[x.artist] = st.skips.get(x.artist, 0) + 1
            _signal(st.neg, x.key)
            if x.key in st.pos:
                st.pos.remove(x.key)
        elif e.kind in ("finish", "like"):
            _signal(st.pos, x.key)
            if x.key in st.neg:
                st.neg.remove(x.key)
            liked.append(x)
            if e.kind == "finish":
                await _download(sid, rt, x)
    radios: list[tuple[str, build.Gen]] = []
    for x in liked:
        if x.ref in st.radios or not x.track:
            continue
        st.radios.append(x.ref)
        t = CatalogTrack.model_validate(x.track)
        radios.append((SESSION_RADIO, lambda t=t: radio_of(t)))
    await _gather(sid, rt, radios, lambda: False)


async def _download(sid: str, rt: Runtime, x: Played) -> None:
    """A track played to its end that the library lacks: a quiet download (`query_json.wave`), unless one is under
    way already (the ♥ one, another session's)."""
    from app.jobs.downloads import query_from_track

    st = rt.state
    if (not get_settings().cfg.storage.autodownload_listened or not rt.download_finished or x.nd_song_id
            or not x.track or x.ref in st.downloads or not x.ref.startswith("dz:")):
        return
    st.downloads.append(x.ref)
    t = CatalogTrack.model_validate(x.track)
    async with session_scope() as s:
        active = (await s.execute(select(DownloadItem.id).where(
            DownloadItem.status.in_(ACTIVE_STATUSES),
            func.json_extract(DownloadItem.query_json, "$.meta.track_ref") == x.ref).limit(1))).scalar_one_or_none()
        if active is not None:
            return
        from app.library.owned import find_existing

        if await find_existing(s, artist=t.artist, title=t.title, duration_s=t.duration_s, isrc=t.isrc,
                               deezer_track_id=t.id) is not None:
            return
        row = await s.get(WaveSession, sid)
        if row is None:
            return
        if row.batch_id is None:
            batch = DownloadBatch(user_id=rt.user_id, origin="wave", title="Моя волна")
            s.add(batch)
            await s.flush()
            row.batch_id = batch.id
        item = DownloadItem(batch_id=row.batch_id, user_id=rt.user_id, query_json={
            "query": query_from_track(t).model_dump(),
            "meta": {"track_ref": x.ref, "deezer_track_id": t.id, "artists": t.artists or [t.artist]},
            "wave": {"session": sid}})
        s.add(item)
        await s.flush()
        await enqueue(s, "download_item", {"item_id": item.id})
    notify()
    log.info("wave %s: played to the end, downloading %s — %s", sid, t.artist, t.title)


async def record(s: AsyncSession, sid: str, user_id: int, raw: list[Any]):
    """The session's events into the listening history (`play_events`, context `wave:<id>`); one the client stored
    already through `/api/plays/events` (the same `event_id`) gets that context. Returns the stored events too."""
    from app.plays import record_events

    context = f"wave:{sid}"
    events = [{**x, "context": context} if isinstance(x, dict) else x for x in raw]
    out = await record_events(s, user_id, events)
    ids = [x["event_id"] for x in events if isinstance(x, dict) and isinstance(x.get("event_id"), str)]
    rows: list[PlayEvent] = []
    if ids:
        await s.execute(update(PlayEvent).where(PlayEvent.user_id == user_id, PlayEvent.event_id.in_(ids),
                        or_(PlayEvent.context.is_(None), ~PlayEvent.context.like("wave:%"), PlayEvent.context == context))
                        .values(context=context))
        rows = list((await s.execute(select(PlayEvent).where(PlayEvent.user_id == user_id,
                                                             PlayEvent.event_id.in_(ids), PlayEvent.context == context)
                                     .order_by(PlayEvent.ts, PlayEvent.id))).scalars())
    return out, rows


# --- the API's two calls --------------------------------------------------------------------------------------------


def title_of(seed: dict[str, Any] | None) -> str:
    if not seed:
        return "Моя волна"
    return f"Волна по треку «{seed.get('title')}»" if seed.get("ref") else f"Волна: {seed.get('artist')}"


@dataclass
class Batch:
    session_id: str
    title: str
    mode: str
    seed: dict[str, Any] | None
    tracks: list[build.Cand]
    keep: list[str]
    add: list[build.Cand]
    pending: bool  # sources still answering in the background
    recorded: Any = None
    target_len: int = BATCH
    filters: WaveFilters = field(default_factory=WaveFilters)
    filter_relaxed: list[str] = field(default_factory=list)


async def _artist_songs(nd, artist_id: str, name: str) -> list[CatalogTrack]:
    """The Navidrome artist's songs, as catalog-like tracks (their Deezer record when the Hub has one)."""
    artist = await nd.get_artist(artist_id)
    albums = [a["id"] for a in artist.get("album") or [] if a.get("id")][:ARTIST_ALBUMS_MAX]
    found = await asyncio.gather(*(nd.get_album(a) for a in albums), return_exceptions=True)
    key = artist_key(name)
    songs: dict[str, dict[str, Any]] = {}
    for album in found:
        if isinstance(album, BaseException):
            continue
        for x in album.get("song") or []:
            ids = {x.get("artistId"), *(a.get("id") for a in x.get("artists") or [])}
            if x.get("id") and (artist_id in ids or artist_key(x.get("artist")) == key):
                songs.setdefault(x["id"], x)
    picked = random.sample(list(songs), min(ARTIST_SONGS_MAX, len(songs)))
    async with session_scope() as s:
        rows = {t.nd_song_id: (t, genre) for t, genre in await s.execute(
            select(Track, Album.genre).outerjoin(Album, Track.album_id == Album.id).where(Track.nd_song_id.in_(picked)))}
    out = []
    for sid in picked:
        if (row := rows.get(sid)) is not None:
            out.append(library_track(*row))
        else:
            x = songs[sid]
            out.append(CatalogTrack(ref=f"nd:{sid}", source="nd", id=sid, title=x.get("title") or "",
                                    artist=x.get("artist") or name, artists=[x.get("artist") or name],
                                    album_title=x.get("album"), duration_s=x.get("duration"), genre=x.get("genre")))
    return out


async def _catalog_artist(name: str, songs: list[CatalogTrack]) -> str | None:
    """The Deezer artist of a library artist: from a song's catalog record, else the search by the exact name."""
    if svc.deezer is None:
        return None
    key = artist_key(name)
    for t in songs:
        if t.source == "dz" and (full := await catalog_track(t.ref)) is not None and full.artist_ref                 and artist_key(full.artist) == key:
            return full.artist_ref
    try:
        found = await svc.deezer.search(name, kinds=("artist",), limit=5)
    except Exception as e:  # The wave goes on with the library songs.
        log.warning("wave: Deezer artist search %r failed: %s", name, e)
        return None
    return next((a.ref for a in found.artists if artist_key(a.name) == key), None)


async def _seed(seed: dict[str, Any], user: User | None = None) -> tuple[CatalogTrack | None, dict[str, Any] | None]:
    """The track or the artist a wave starts from."""
    if seed.get("by_artist"):
        nd_id = seed.get("nd_song_id")
        ref = seed.get("ref") or ""
        if ref.startswith("nd:"):
            nd_id = ref[3:]
        if nd_id and user is not None:
            from app.auth import user_navidrome

            nd = user_navidrome(user)
            try:
                artist_id = (await nd.get_song(nd_id)).get("artistId")
            except Exception as e:
                raise WaveError(404, "Исполнитель трека не найден") from e
            finally:
                await nd.aclose()
            if artist_id:
                return await _seed({"nd_artist_id": artist_id}, user)
        elif ref.startswith("dz:"):
            track = await catalog_track(ref)
            if track and track.artist_ref:
                return await _seed({"artist_ref": track.artist_ref}, user)
        raise WaveError(404, "Исполнитель трека не найден")
    if nd := seed.get("nd_song_id"):
        async with session_scope() as s:
            row = (await s.execute(select(Track, Album.genre).outerjoin(Album, Track.album_id == Album.id)
                                   .where(Track.nd_song_id == nd).limit(1))).first()
            lt = library_track(*row) if row is not None else None
        if lt is None:
            raise WaveError(404, "Песни нет в библиотеке")
        if lt.source == "dz" and (full := await catalog_track(lt.ref)) is not None:
            lt = full.model_copy(update={"genre": lt.genre or full.genre})  # the artist's ref for its radio
        return lt, None
    if ref := seed.get("ref"):
        if ref.startswith("nd:"):
            return await _seed({"nd_song_id": ref[3:]}, user)
        if ref.startswith("file:"):
            from app import listen

            c = await listen.file_candidate(ref)
            if c is None or not c.artist.strip() or not c.title.strip():
                raise WaveError(404, "Файл не найден: повторите поиск")
            return CatalogTrack(ref=ref, source="file", id=c.provider_id, title=c.title, artist=c.artist,
                                artists=[c.artist], album_title=c.album, duration_s=c.duration_s,
                                explicit=c.explicit, cover_url=c.cover_url, cover_small_url=c.cover_url), None
        if not ref.startswith("dz:") or (t := await catalog_track(ref)) is None:
            raise WaveError(404, "Трек не найден в каталоге")
        return t, None
    if (ref := seed.get("artist_ref")) and svc.deezer is not None:
        try:
            a = await svc.deezer.get_artist(split_ref(ref)[1])
        except Exception as e:
            raise WaveError(404, "Исполнитель не найден") from e
        return None, {"ref": a.ref, "name": a.name}
    if (nd_id := seed.get("nd_artist_id")) and user is not None:
        from app.auth import user_navidrome

        nd = user_navidrome(user)
        try:
            name = (await nd.get_artist(nd_id)).get("name")
            if not name:
                raise WaveError(404, "Исполнитель не найден")
            songs = await _artist_songs(nd, nd_id, name)
        except WaveError:
            raise
        except Exception as e:
            raise WaveError(404, "Исполнитель не найден") from e
        finally:
            await nd.aclose()
        return None, {"ref": await _catalog_artist(name, songs), "name": name, "nd_artist_id": nd_id, "songs": songs}
    raise WaveError(400, "Нужен ref, nd_song_id, artist_ref или nd_artist_id")


async def clear_old(s: AsyncSession, now: datetime | None = None) -> int:
    """Sessions not asked for `KEEP`: their state becomes a summary (`CLEARED_KEYS`; the pool, the queue and the
    played tracks are heavy and no longer needed), the row and its `wave_items` stay for the history. Over already
    (`IDLE`): such a session answers 404 as before. Returns how many were cleared."""
    now = now or utcnow()
    rows = list((await s.execute(select(WaveSession).where(WaveSession.updated_at < now - KEEP,
                                                           WaveSession.cleared_at.is_(None)))).scalars())
    for row in rows:
        old = row.state_json or {}
        row.state_json = {"cleared": True, **{k: old[k] for k in CLEARED_KEYS if k in old}}
        row.cleared_at = now
        if (rt := _rt.pop(row.id, None)) is not None:
            for t in rt.tasks:
                t.cancel()
    return len(rows)


async def _start_pool(sid: str, rt: Runtime, gens: list[tuple[str, build.Gen]],
                      deadline: float) -> build.Cand | None:
    """Absorb sources as they answer; prefer a local candidate or verify a catalog pick by the deadline."""
    pending = {asyncio.create_task(build.run_generators({name: gen})) for name, gen in gens}
    first = None
    probe_task: asyncio.Task | None = None
    try:
        while time.monotonic() < deadline:
            if probe_task is None:
                local = pick(rt.state, rt.profile, rt.mode, rt.vectors, n=1, eligible=_guaranteed_full)
                if local:
                    first = local[0]
                    break
                candidates = pick(rt.state, rt.profile, rt.mode, rt.vectors, n=1,
                                  eligible=lambda c: _can_probe(rt, c))
                if candidates:
                    probe_deadline = min(deadline, time.monotonic() + FIRST_PROBE_TIMEOUT_S)
                    probe_task = asyncio.create_task(_probe_full(rt, candidates, deadline=probe_deadline))
            wait_set = pending | ({probe_task} if probe_task is not None else set())
            if not wait_set:
                break
            # The probe enforces its own deadline and records a cooldown if time runs out.
            timeout = max(0.0, deadline - time.monotonic()) if probe_task is None else None
            done, _ = await asyncio.wait(wait_set, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            source_done = done & pending
            if source_done:
                pending.difference_update(source_done)
                async with rt.lock:
                    await absorb(rt, [r for task in source_done for r in task.result()])
                    await (rt.save or _save)(sid, rt)
                local = pick(rt.state, rt.profile, rt.mode, rt.vectors, n=1, eligible=_guaranteed_full)
                if local:
                    first = local[0]
                    break
            if probe_task in done:
                checked = probe_task.result()
                probe_task = None
                if checked:
                    first = checked[0]
                    break
    finally:
        if probe_task is not None:
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)
        for task in pending:
            late = asyncio.create_task(_late(sid, rt, task))
            rt.tasks.add(late)
            late.add_done_callback(rt.tasks.discard)
    return first


async def start(user: User, seed: dict[str, Any] | None, mode: str | None, *, origin: str | None = None,
                client: str | None = None, restart_of: str | None = None,
                request: dict[str, Any] | None = None, filters: WaveFilters | None = None) -> Batch:
    """A new session. `origin`, `client`, `restart_of` and `request` (the start's body as it came) are kept for the
    analytics only."""
    from app.mixes.core import settings_of

    total_started = time.monotonic()
    timings: dict[str, float] = {}
    cfg = get_settings().cfg.mixes
    async with session_scope() as s:
        await clear_old(s)
        mode = mode or (await settings_of(s, user.id)).mode
        exclusions = list((await s.execute(select(WaveExclusion).where(
            WaveExclusion.user_id == user.id, WaveExclusion.expires_at > utcnow()))).scalars())
    seed_track, seed_artist = await _seed(seed, user) if seed else (None, None)
    stage = time.monotonic()
    try:
        async with session_scope() as s:
            profile = await asyncio.wait_for(build_profile(s, user, model=cfg.taste_model), PROFILE_TIMEOUT_S)
    except TimeoutError:
        log.warning("wave: build_profile timed out after %.1fs; using minimal profile", PROFILE_TIMEOUT_S)
        profile = Profile(user_id=user.id, now=utcnow())
    timings["build_profile"] = time.monotonic() - stage
    sid = uuid.uuid4().hex[:16]
    if isinstance(profile, V2Profile):
        from app.mixes.taste_select import select_profile_seeds
        profile_seeds = select_profile_seeds(profile, cfg.seeds, salt=sid)
    else:
        profile_seeds = None
    filters = filters or WaveFilters()
    active = wf.active(filters)
    seeds: list[TasteTrack] = []
    if seed_track is None and seed_artist is None:
        if isinstance(profile, V2Profile):
            seeds = profile_seeds or []
        else:
            seeds = profile.seeds(cfg.seeds * 4 if active else cfg.seeds) or await build.cold_seeds(profile, cfg.seeds)
        if profile.empty or not seeds:
            raise WaveError(409, "Волне не от чего оттолкнуться: послушайте что-нибудь или выберите исполнителей")
        if active:
            if isinstance(profile, V2Profile):
                # Include alternatives beyond the geometry shortlist, preserving its order for ties.
                known = {t.key for t in seeds}
                seeds += [t for t in profile.seeds(cfg.seeds * 4) if t.key not in known]
            seeds = (await _prioritize_seeds(profile, seeds, filters))[:cfg.seeds]
    rt = Runtime(user_id=user.id, mode=mode, state=State(taste_model=cfg.taste_model, filters=filters), profile=profile,
                 salt=sid)
    rt.state.exclusion_refs = {x.ref for x in exclusions if x.reason != "no_file"}
    rt.state.exclusion_fps = {x.fingerprint for x in exclusions if x.fingerprint and x.reason != "no_file"}
    rt.state.skipped_refs = {x.ref for x in exclusions if x.reason == "skipped"}
    rt.state.skipped_fps = {x.fingerprint for x in exclusions if x.fingerprint and x.reason == "skipped"}
    rt.state.nofile.update(x.ref for x in exclusions if x.reason == "no_file")
    rt.state.no_file_fps = {x.fingerprint for x in exclusions if x.reason == "no_file" and x.fingerprint}
    rt.state.seeds_used = [t.key for t in seeds]
    rt.state.seed_artist = artist_key(seed_artist["name"]) if seed_artist else None
    seed_json = ({"ref": seed_track.ref, "artist": seed_track.artist, "title": seed_track.title} if seed_track
                 else {k: v for k, v in (("artist_ref", seed_artist.get("ref")), ("artist", seed_artist["name"]),
                                         ("nd_artist_id", seed_artist.get("nd_artist_id"))) if v}
                 if seed_artist else None)
    async with session_scope() as s:
        s.add(WaveSession(id=sid, user_id=user.id, mode=mode, seed_json=seed_json, state_json={}, origin=origin,
                          client=client, restart_of=restart_of, taste_model=cfg.taste_model, request_json=request))
    _rt[sid] = rt
    first = None
    if seed_track is not None:
        first = build.Cand(track=seed_track, gens={"seed": 0})
        await build.link_library({seed_track.ref: first})
        if _disliked(first, profile):
            first = None
        else:
            first.familiar = build.keep(first, profile, cfg) and first.familiar
            first.score = build.score(first, weights(), profile)
            if active:
                await wf.prepare([first], filters)
                await _fetch_energy(rt.state, [first])
                if not wf.matches(first, filters, energy=rt.state.energy):
                    rt.state.put(first)
                    first = None
    gens = list((generators(profile, seed_track, seed_artist, seeds, filters=filters) if active
                 else generators(profile, seed_track, seed_artist, seeds)).items())
    stage = time.monotonic()
    confirmed_first = None
    if first is not None:
        # The seed isn't heard until playback starts. `first` blocks duplicate picks, and once
        # in `out` it also blocks duplicates from the late sources.
        await _start_pool(sid, rt, gens, time.monotonic())
    else:
        first = confirmed_first = await _start_pool(sid, rt, gens, time.monotonic() + START_FIRST_TIMEOUT_S)
    timings["_gather/probe"] = time.monotonic() - stage
    async with rt.lock:
        stage = time.monotonic()
        keep, kept, added = await _hand_out(sid, rt, [], first, first_only=seed_track is None,
                                          confirmed_first=confirmed_first)
        timings["_hand_out"] = time.monotonic() - stage
        pending = bool(rt.tasks)
    if isinstance(profile, V2Profile) and "exhausted" not in rt.filter_relaxed:
        from app.mixes.taste_select import prepare

        async def prepare_profile() -> None:
            async with rt.lock:
                await prepare(profile)
                await (rt.save or _save)(sid, rt)

        task = asyncio.create_task(prepare_profile())
        rt.tasks.add(task)
        task.add_done_callback(rt.tasks.discard)
        pending = True
    save_started = time.monotonic()
    await _save(sid, rt, force=True)
    timings["_save"] = time.monotonic() - save_started
    timings["total"] = time.monotonic() - total_started
    log.info("wave %s: start total=%.2fs build_profile=%.2f _gather/probe=%.2f _hand_out=%.2f _save=%.2f "
             "state=%dKB", sid, timings["total"], timings["build_profile"], timings["_gather/probe"],
             timings["_hand_out"], timings["_save"], getattr(rt, "last_state_kb", 0))
    return Batch(sid, title_of(seed_json), mode, seed_json, kept + added, keep, added,
                 pending or (not active and not added), filters=filters, filter_relaxed=rt.filter_relaxed)


async def next_batch(user: User, sid: str, played: list[Any], queued: list[str]) -> Batch:
    total_started = time.monotonic()
    timings: dict[str, float] = {}
    rt = await _runtime(sid, user)
    if not rt.state.pool and rt.tasks:  # nothing to pick from yet: the sources still answer (outside the lock)
        await asyncio.wait(set(rt.tasks), timeout=min(get_settings().wave_wait_s, SOURCE_WAIT_S))
    if not rt.state.pool and not rt.tasks and rt.profile is None and not wf.active(rt.state.filters):
        async with session_scope() as s:
            row = await s.get(WaveSession, sid)
            if row is not None and row.seed_json and row.seed_json.get("ref") and not rt.state.heard:
                seed_track, _ = await _seed({"ref": row.seed_json["ref"]}, user)
                if seed_track is not None:
                    rt.state.pool[seed_track.ref] = build.Cand(track=seed_track, gens={"seed": 0})
    async with rt.lock:
        async with session_scope() as s:
            recorded, events = await record(s, sid, user.id, played)
            if rt.state.taste_model == "v2":
                events = list((await s.execute(select(PlayEvent).where(PlayEvent.user_id == user.id,
                    PlayEvent.context == f"wave:{sid}", PlayEvent.ts <= utcnow())
                    .order_by(PlayEvent.ts, PlayEvent.id))).scalars())
            row = await s.get(WaveSession, sid)
            seed_json = row.seed_json if row else None
            if row is not None:
                row.next_calls = (row.next_calls or 0) + 1
        await _profile(rt, user, timings=timings)  # after events and external state changes
        stage = time.monotonic()
        await learn(sid, rt, events)
        timings["learn/gather"] = time.monotonic() - stage
        st = rt.state
        if st.filters.familiarity and isinstance(rt.profile, V2Profile):
            for c in [*st.pool.values(), *st.pool_relaxed.values()]:
                if build.keep(c, rt.profile, get_settings().cfg.mixes):
                    st.put(c)
                else:
                    st.remove(c.track.ref)
        if len(st.pool) < POOL_LOW:
            _refill(sid, rt)
        stage = time.monotonic()
        keep, kept, added = await _hand_out(sid, rt, queued)
        tracks = kept + added
        timings["hand_out"] = time.monotonic() - stage
        timings["_vectors"] = getattr(rt, "_wave_vector_elapsed", 0.0)
        timings["_probe"] = getattr(rt, "_wave_probe_elapsed", 0.0)
        timings["_save"] = getattr(rt, "_wave_save_elapsed", 0.0)
        save_started = time.monotonic()
        await _save(sid, rt, force=bool(played))
        timings["_save"] += time.monotonic() - save_started
        pending = bool(rt.tasks) or (not tracks and not wf.active(st.filters))
        filter_relaxed = rt.filter_relaxed
    timings["total"] = time.monotonic() - total_started
    log.info("wave %s: next total=%.2fs _profile=%.2f _vectors=%.2f _gather=%.2f _probe=%.2f _hand_out=%.2f "
             "_save=%.2f state=%dKB", sid, timings["total"], timings.get("_profile", 0.0),
             timings.get("_vectors", 0.0), timings.get("learn/gather", 0.0), timings.get("_probe", 0.0),
             timings.get("hand_out", 0.0), timings.get("_save", 0.0), getattr(rt, "last_state_kb", 0))
    return Batch(sid, title_of(seed_json), rt.mode, seed_json, tracks, keep, added, pending, recorded,
                 filters=st.filters, filter_relaxed=filter_relaxed)


def _can_probe(rt: Runtime, c: build.Cand) -> bool:
    return rt.probe_cooldown.get(c.track.ref, 0) <= time.monotonic()


async def _probe_full(rt: Runtime, tracks: list[build.Cand], *, deadline: float,
                      confirmed: build.Cand | None = None) -> list[build.Cand]:
    from app import listen

    now = time.monotonic()
    semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)
    for ref, until in list(rt.probe_cooldown.items()):
        if until <= now:
            rt.probe_cooldown.pop(ref, None)
    st = rt.state
    tracks = [c for c in tracks if not _known_preview(st, c)]
    full = {c.track.ref for c in tracks if c is confirmed or _guaranteed_full(c)}
    probe = [c for c in tracks if c.track.ref not in full and _can_probe(rt, c)]

    async def one(c: build.Cand):
        try:
            async with semaphore:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                choice = await asyncio.wait_for(
                    listen.choose(c.track.ref, background=True, cancel_resolution_on_cancel=True), remaining)
                return c, choice, None
        except TimeoutError:
            log.info("wave probe timed out: ref=%s", c.track.ref)
            return c, None, TimeoutError()
        except listen.ListenError as e:
            return c, None, e
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return c, None, e

    if not probe or time.monotonic() >= deadline:
        return [c for c in tracks if c.track.ref in full]

    tasks = {asyncio.create_task(one(c)): c for c in probe}
    done = set()
    try:
        done, _ = await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()))
    finally:
        for task in tasks.keys() - done:
            task.cancel()
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    results = [task.result() for task in done if not task.cancelled() and task.exception() is None]
    absent = set()
    transient = {c.track.ref for c in probe} - {c.track.ref for c, _, _ in results}
    for c, choice, error in results:
        if choice is not None and choice.kind == "preview":
            absent.add(c.track.ref)
        elif choice is not None and choice.kind in ("full", "library"):
            full.add(c.track.ref)
        else:
            transient.add(c.track.ref)
    if transient:
        until = time.monotonic() + WAVE_TRANSIENT_TTL.total_seconds()
        rt.probe_cooldown.update({ref: until for ref in transient})
    rejected = [c for c in tracks if c.track.ref in absent]
    for c in rejected:
        rt.state.nofile.add(c.track.ref)
        rt.state.exclusion_refs.add(c.track.ref)
        rt.state.exclusion_fps.add(fp_of(c))
        rt.state.no_file_fps.add(fp_of(c))
        rt.state.remove(c.track.ref)
        rt.state.out.pop(c.track.ref, None)
    await _write_exclusions(rt.user_id, rejected, "no_file", WAVE_NO_FILE_TTL)
    return [c for c in tracks if c.track.ref in full and not _known_preview(st, c)]


async def _hand_out(sid: str, rt: Runtime, queued: list[str], first: build.Cand | None = None,
                    *, first_only: bool = False,
                    confirmed_first: build.Cand | None = None) -> tuple[list[str], list[build.Cand], list[build.Cand]]:
    """Keep valid queued tracks in client order, then pick and prepare only the missing tracks."""
    from app import listen

    assert rt.profile is not None
    st = rt.state
    active = wf.active(st.filters)
    rt.filter_relaxed = []
    if isinstance(rt.profile, V2Profile):
        cfg = get_settings().cfg.mixes
        for ref, c in list(st.pool.items()):
            if not build.keep(c, rt.profile, cfg):
                del st.pool[ref]
        _session_weights(st, rt.profile)
    else:
        cfg = None
    keep: list[str] = []
    kept: list[build.Cand] = []
    seen = set(st.heard)
    for ref in queued:
        c = _match_out(st, ref)
        if c is None:
            continue
        fp = fp_of(c)
        if (fp in seen or _disliked(c, rt.profile) or ref in st.nofile or c.track.ref in st.nofile
                or fp in st.no_file_fps or ref in st.skipped_refs or _skipped(st, c, fp)
                or (cfg is not None and not build.keep(c, rt.profile, cfg))
                or (st.filters.lang and wf.detect_lang(c) != st.filters.lang)):
            continue
        seen.add(fp)
        keep.append(ref)
        kept.append(c)
    n = (1 if first_only else BATCH) - len(kept)
    values: dict[str, float] = {}
    now = time.time()
    st.prune_handed(now)
    rt._wave_vector_elapsed = 0.0
    if first_only or n <= 0:
        vectors = rt.vectors
    else:
        try:
            stage = time.monotonic()
            vectors = await asyncio.wait_for(_vectors(rt), TASTE_TIMEOUT_S)
            rt._wave_vector_elapsed = time.monotonic() - stage
        except TimeoutError:
            log.warning("wave: taste vectors timed out; continuing without taste weights")
            vectors = {}
    probe_started = time.monotonic()
    deadline = probe_started + (FIRST_PROBE_TIMEOUT_S if first_only else PROBE_BATCH_TIMEOUT_S)
    added: list[build.Cand] = []
    attempted: set[str] = set()

    def eligible(c: build.Cand) -> bool:
        return c.track.ref not in attempted and (c is confirmed_first or _guaranteed_full(c)
                                                  or (time.monotonic() < deadline and _can_probe(rt, c)))

    async def fill(level: int, preferred: build.Cand | None = None) -> None:
        while len(added) < n:
            extras = pick(st, rt.profile, rt.mode, vectors, n=n - len(added), first=preferred, values=values,
                          prefix=(*kept, *added), filter_level=level, eligible=eligible)
            if not extras:
                break
            attempted.update(c.track.ref for c in extras)
            added.extend(await _probe_full(rt, extras, deadline=deadline, confirmed=confirmed_first))
            preferred = None

    await fill(0, first)
    if active and not rt.tasks:
        for level in ((1, 2, 3, 4) if st.filters.energy else (1, 3, 4)):
            if len(added) >= n or rt.tasks:
                break
            await fill(level)
    # A confirmed preview (including one previously reported by a client) cannot survive the handout.
    queued_ok = [(ref, c) for ref, c in zip(keep, kept) if not _known_preview(st, c)]
    keep = [ref for ref, _ in queued_ok]
    kept = [c for _, c in queued_ok]
    added = [c for c in added if not _known_preview(st, c)]
    if active:
        rt.filter_relaxed = wf.relaxed_axes(kept + added, st.filters, energy=st.energy)
        if not kept and not added and not rt.tasks:
            rt.filter_relaxed.append("exhausted")
    rt._wave_probe_elapsed = time.monotonic() - probe_started
    if not kept and not added and (first is None or active):
        # Nothing fresh is left: rather than repeating what the client just flipped past, refill and let it ask
        # again (`pending` in the answer; the client waits `RETRY_MS`). Its queue stays as it is: clearing `out`
        # would leave the next `queued` naming tracks this session no longer knows.
        if not active:
            _refill(sid, rt)
        save_started = time.monotonic()
        await _save(sid, rt)
        rt._wave_save_elapsed = time.monotonic() - save_started
        return keep, kept, added
    new_out = {c.track.ref: c for c in [*kept, *added]}
    for ref, c in st.out.items():
        if ref not in new_out:
            st.put(c)
    for ref in new_out:
        st.remove(ref)
    st.trim_reserve()
    st.out = new_out
    for c in added:
        st.handed[c.track.ref] = now
    listen.pin(rt.user_id, [*keep, *(c.track.ref for c in added)])
    if added:
        await _write_exclusions(rt.user_id, added, "shown", WAVE_SHOWN_TTL)
    save_started = time.monotonic()
    await _save(sid, rt, force=st.started == 0)
    rt._wave_save_elapsed = time.monotonic() - save_started
    if added:
        try:  # an analytics failure never costs the wave its answer or state
            async with session_scope() as s:
                await save_items(s, sid, rt.user_id, added, values)
        except Exception:
            log.exception("wave %s: handed out tracks not recorded", sid)
    stream = [c.track.ref for c in [*kept, *added] if not c.nd_song_id and c.track.source != "nd"]
    # Keep pending resolutions for retained refs, but don't pick evicted refs later.
    listen.prefetch(rt.user_id, stream)
    if added:
        # Library songs are warmed directly, others after their files load.
        listen.warm_light([f"nd:{c.nd_song_id}" if c.nd_song_id else c.track.ref for c in added
                           if c.nd_song_id or c.track.source != "nd"])
    return keep, kept, added


async def save_items(s: AsyncSession, sid: str, user_id: int, tracks: list[build.Cand],
                     values: dict[str, float], now: datetime | None = None) -> None:
    """What the wave handed out into `wave_items` (INSIGHTS.md 6.4): a track new to the session gets the next
    position; one newly handed out again after going back to the pool counts once more,
    with its latest score, closeness and value."""
    if not tracks:
        return
    now = now or utcnow()
    refs = [c.track.ref for c in tracks]
    have = set((await s.execute(select(WaveItem.ref).where(WaveItem.session_id == sid,
                                                          WaveItem.ref.in_(refs)))).scalars())
    nxt = (await s.execute(select(func.coalesce(func.max(WaveItem.position) + 1, 0))
                           .where(WaveItem.session_id == sid))).scalar_one()
    rows = []
    for c in tracks:
        ref = c.track.ref
        rows.append(dict(session_id=sid, user_id=user_id, first_handed_at=now, last_handed_at=now, handed_count=1,
                         position=-1 if ref in have else nxt, ref=ref, nd_song_id=c.nd_song_id, key=c.key,
                         artist=(c.track.artist or "")[:256], title=(c.track.title or "")[:256],
                         generator=top_generator(c)[:32], gens=dict(c.gens), familiar=bool(c.familiar),
                         score=c.score, sim=c.sim, value=values.get(ref)))
        if ref not in have:
            have.add(ref)
            nxt += 1
    q = sqlite_insert(WaveItem).values(rows)
    await s.execute(q.on_conflict_do_update(index_elements=["session_id", "ref"], set_={
        "handed_count": WaveItem.handed_count + 1, "last_handed_at": q.excluded.last_handed_at,
        "score": q.excluded.score, "sim": q.excluded.sim, "value": q.excluded.value}))


async def no_file(user: User, sid: str, ref: str) -> bool:
    """A wave track no source has (the client got a 30 s preview): it leaves the session's queue and pool for good.
    False — the session is not this user's or the track is not one it handed out."""
    rt = await _runtime(sid, user)
    async with rt.lock:
        st = rt.state
        c = _match_out(st, ref) or st.pool.get(ref) or st.pool_relaxed.get(ref)
        # The client may report the alias it received in `keep`, not the canonical ref in `out`.
        if c is not None:
            ref = c.track.ref
            st.out.pop(ref, None)
            st.remove(ref)
        if c is None and rt.profile is not None:
            taste_track = rt.profile.find(ref=ref)
            if taste_track is not None:
                c = build.Cand(track=taste_track.track, gens={})
        st.handed.pop(ref, None)
        st.nofile.add(ref)
        st.exclusion_refs.add(ref)
        if c is not None:
            st.exclusion_fps.add(fp_of(c))
            st.no_file_fps.add(fp_of(c))
        await _save(sid, rt, force=True)
        await _write_exclusion(rt.user_id, ref, fp_of(c) or None if c is not None else None,
                               "no_file", WAVE_NO_FILE_TTL)
        if c is not None:
            log.info("wave %s: no source has %s — %s", sid, c.track.artist, c.track.title)
        return True


async def settle(sid: str) -> None:
    """Waits for the session's background sources (tests)."""
    rt = _rt.get(sid)
    while rt is not None and rt.tasks:
        tasks = list(rt.tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        # gather() can finish synchronously for done tasks, before their discard callbacks run.
        # Remove this completed snapshot ourselves; tasks spawned while awaiting remain tracked.
        rt.tasks.difference_update(tasks)


async def reset() -> None:
    """Forget the sessions in memory and stop their tasks (shutdown, tests): they load from the database again."""
    for rt in _rt.values():
        for t in rt.tasks:
            t.cancel()
    _rt.clear()


def generator_title(name: str) -> str:
    return TITLES.get(name) or build.GENERATOR_TITLES.get(name, name)


def top_generator(c: build.Cand) -> str:
    w = weights()
    return max(c.gens, key=lambda g: w.get(g, 0.5) / math.log2(c.gens[g] + 2)) if c.gens else "seed"


__all__ = ["BATCH", "Batch", "WaveError", "generator_title", "next_batch", "reset", "settle", "start"]
