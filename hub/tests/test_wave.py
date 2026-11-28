"""«Моя волна» (app/mixes/wave.py, api/wave.py): picking (an artist once in 8 tracks, the mode's share, the session's
skips and likes, by sound), learning from the session's events, the session over the API — the first 5, the seed
first, events into the history, «Не нравится», the quiet download, the radio of a liked track, a restart.

The generators are fakes (`fake_sources`; the real ones and the filters are the mix's: test_mix_build.py,
test_mix_sources.py); Navidrome is the mock of test_mixes.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from sqlalchemy import select

from app.api.schemas import WaveFilters
from app.catalog.models import CatalogTrack
from app.mixes import build, wave
from app.mixes.taste import Profile, artist_key
from tests.support import FakeDeezer, app_client
from tests.test_mixes import ND, add_user, history, user_id

NOW = datetime(2026, 10, 23, 12, 0)


def tr(n: int, artist: str | None = None) -> CatalogTrack:
    a = artist or f"Артист {n}"
    return CatalogTrack(ref=f"dz:{n}", source="dz", id=str(n), title=f"Песня {n}", artist=a, artists=[a],
                        artist_ref=f"dz:a-{a}", duration_s=200)


def cand(n: int, artist: str | None = None, *, score: float = 1.0, familiar: bool = False) -> build.Cand:
    return build.Cand(track=tr(n, artist), gens={"ytm_radio": 0}, familiar=familiar, score=score)


def state(*cands: build.Cand, played: list[str] = ()) -> wave.State:
    st = wave.State()
    st.pool = {c.track.ref: c for c in cands}
    for i, a in enumerate(played):
        c = cand(90_000 + i, a)
        st.played.append(wave._as_played(c))
    return st


def profile() -> Profile:
    return Profile(user_id=1, now=NOW)


# --- picking --------------------------------------------------------------------------------------------------------


def test_an_artist_plays_at_most_once_in_eight_tracks() -> None:
    # 3 songs of each of 8 artists; «A» and «B» played just now
    cands = [cand(i * 10 + k, f"Арт {chr(65 + i)}", score=10 - k - i / 10) for i in range(8) for k in range(3)]
    st = state(*cands, played=["Арт A", "Арт B"])

    got = wave.pick(st, profile(), "discover", {}, n=5)

    seq = ["Арт A", "Арт B", *(c.track.artist for c in got)]
    assert len(got) == 5
    assert all(len(set(seq[i:i + 8])) == len(seq[i:i + 8]) for i in range(len(seq)))  # no artist twice in 8


def test_the_gap_gives_way_when_nothing_else_is_left() -> None:
    st = state(*(cand(i, "Арт A", score=5 - i / 10) for i in range(3)), *(cand(10 + i, "Арт B") for i in range(3)),
               played=["Арт A"])

    got = [c.track.artist for c in wave.pick(st, profile(), "discover", {}, n=5)]

    assert got == ["Арт B", "Арт A", "Арт B", "Арт A", "Арт B"]  # never the same artist twice in a row


@pytest.mark.parametrize(("mode", "familiar"), [("normal", 2), ("favorites", 4), ("discover", 1)])
def test_the_mode_keeps_the_familiar_share(mode: str, familiar: int) -> None:
    st = state(*(cand(i, score=5, familiar=True) for i in range(10)), *(cand(100 + i, score=5) for i in range(10)))

    got = wave.pick(st, profile(), mode, {}, n=5)

    assert sum(c.familiar for c in got) == familiar


@pytest.mark.parametrize(("lang", "ref"), [("ru", "dz:1"), ("foreign", "dz:2")])
def test_a_lang_filter_drops_the_other_language(lang, ref):
    st = state(cand(1), cand(2, "Foreign", score=99))
    st.pool["dz:2"].track.title = "Song"
    st.filters = WaveFilters(lang=lang)
    assert [c.track.ref for c in wave.pick(st, profile(), "normal", {})] == [ref]


@pytest.mark.parametrize(("familiarity", "expected"), [("known", True), ("new", False)])
def test_a_familiarity_filter_is_hard(familiarity, expected):
    st = state(cand(1, familiar=True), cand(2, score=99), cand(3, score=999))
    st.pool["dz:3"].familiar = None
    st.filters = WaveFilters(familiarity=familiarity)
    got = wave.pick(st, profile(), "favorites" if not expected else "discover", {})
    assert len(got) == 1 and got[0].familiar is expected


def test_genres_are_or_within_and_axes_are_and():
    st = state(*(cand(i, familiar=i != 3) for i in range(5)))
    for c, genre in zip(st.pool.values(), ("Rock", "Jazz", None, "Jazz", "Jazz"), strict=True):
        c.track.genre = genre
    st.pool["dz:1"].track = st.pool["dz:1"].track.model_copy(
        update={"artist": "Foreign", "title": "Song", "artists": ["Foreign"]})
    st.filters = WaveFilters(genres=["rock", "jazz"], lang="ru", familiarity="known", energy="high")
    st.energy = {c.key: (0.1 if c.track.ref == "dz:2" else 0.8) for c in st.pool.values()}
    assert {c.track.ref for c in wave.pick(st, profile(), "normal", {})} == {"dz:0", "dz:4"}


def test_an_empty_filter_behaves_exactly_as_before():
    st = state(*(cand(i, familiar=i < 10) for i in range(20)))
    for mode in build.MODES:
        expected = wave.pick(st, profile(), mode, {})
        st.filters = WaveFilters()
        assert wave.pick(st, profile(), mode, {}) == expected
        st.energy = {c.key: 0.1 for c in st.pool.values()}
        assert wave.pick(st, profile(), mode, {}) == expected


def test_filters_survive_the_state_round_trip():
    st = wave.State(filters=WaveFilters(genres=["jazz", "rock"], lang="ru", familiarity="new", energy="low"))
    assert wave.State.load(st.dump()).filters == st.filters
    assert wave.State.load({}).filters == WaveFilters()


def test_the_artist_of_a_wave_by_an_artist_plays_once_in_three() -> None:
    cands = [cand(i, "Кино", score=10 - i / 10) for i in range(6)] + [cand(100 + i, score=5) for i in range(10)]
    st = state(*cands)
    st.seed_artist = artist_key("Кино")

    got = [c.track.artist for c in wave.pick(st, profile(), "discover", {}, n=9)]

    assert [i for i, a in enumerate(got) if a == "Кино"] == [0, 3, 6]


def test_early_skips_lower_the_artist() -> None:
    st = state(cand(1, "Скучный", score=2.0), cand(2, "Другой", score=1.6))
    st.skips[artist_key("Скучный")] = 1

    assert [c.track.ref for c in wave.pick(st, profile(), "discover", {}, n=2)] == ["dz:2", "dz:1"]


def test_sound_like_the_skipped_goes_down_like_the_liked_goes_up() -> None:
    st = state(cand(1, score=1.0), cand(2, score=1.0), cand(3, score=1.2))
    st.neg, st.pos = ["dz:skipped"], ["dz:liked"]
    vectors = {"dz:skipped": [1.0, 0.0], "dz:liked": [0.0, 1.0], "dz:1": [1.0, 0.0], "dz:2": [0.0, 1.0]}

    got = [c.track.ref for c in wave.pick(st, profile(), "discover", vectors, n=3)]

    assert got == ["dz:2", "dz:3", "dz:1"]  # dz:3 has no vector: its score only


def test_disliked_since_it_came_in_is_not_picked() -> None:
    p = profile()
    p.disliked_ids.add("dz:1")
    p.disliked_artists.add(artist_key("Нелюбимый"))
    st = state(cand(1, score=9), cand(2, "Нелюбимый", score=8), cand(3, score=1))

    assert [c.track.ref for c in wave.pick(st, p, "discover", {}, n=3)] == ["dz:3"]


def test_same_song_with_different_refs_is_only_picked_once() -> None:
    catalog = cand(1, score=10)
    library = build.Cand(
        track=CatalogTrack(ref="nd:song-1", source="nd", id="song-1", title=catalog.track.title,
                           artist=catalog.track.artist, artists=catalog.track.artists),
        gens={"favorites": 0}, score=9,
    )
    st = state(catalog, library, cand(2, score=1))

    got = wave.pick(st, profile(), "discover", {}, n=5)

    same_song = [c for c in got if wave.fp_of(c) == wave.fp_of(catalog)]
    assert len(same_song) == 1
    assert len({wave.fp_of(c) for c in got}) == len(got)


def test_queued_song_alternative_ref_is_not_picked_again() -> None:
    queued = cand(1, score=10)
    alternate = build.Cand(
        track=CatalogTrack(ref="nd:song-1", source="nd", id="song-1", title=queued.track.title,
                           artist=queued.track.artist, artists=queued.track.artists),
        gens={"favorites": 0}, score=9,
    )
    st = wave.State()
    st.out[queued.track.ref] = queued
    st.pool[alternate.track.ref] = alternate

    got = wave.pick(st, profile(), "discover", {}, n=5, queued={queued.track.ref})
    assert got == []


def test_a_queued_track_is_not_swapped_for_a_hair_better_one() -> None:
    st = state(cand(1, score=1.0), cand(2, score=1.1))

    assert [c.track.ref for c in wave.pick(st, profile(), "discover", {}, n=1, queued={"dz:1"})] == ["dz:1"]


def test_a_track_flipped_past_is_not_offered_again() -> None:
    """A track handed out but never started (flipped past) is not in `heard`: without the handout time it would
    come back as if it were new — that is the duplicate a fast skimming produces."""
    import time as clock

    flipped = cand(1, score=10)
    st = state(cand(2, score=1), cand(3, score=1))
    st.pool[flipped.track.ref] = flipped  # it went back to the pool, its `start` never came
    st.handed[flipped.track.ref] = clock.time()

    got = wave.pick(st, profile(), "discover", {}, n=5)

    assert flipped.track.ref not in [c.track.ref for c in got]
    assert [c.track.ref for c in got] == ["dz:2", "dz:3"]


def test_a_flipped_past_track_still_plays_when_the_client_has_it_queued() -> None:
    import time as clock

    kept = cand(1, score=10)
    st = state(kept, cand(2, score=1))
    st.handed[kept.track.ref] = clock.time()

    got = wave.pick(st, profile(), "discover", {}, n=5, queued={kept.track.ref})

    assert kept.track.ref in [c.track.ref for c in got]


def test_a_track_handed_out_long_ago_may_come_again() -> None:
    import time as clock

    old = cand(1, score=10)
    st = state(old, cand(2, score=1))
    st.handed[old.track.ref] = clock.time() - wave.HANDED_TTL_S - 1

    got = wave.pick(st, profile(), "discover", {}, n=5)

    assert old.track.ref in [c.track.ref for c in got]


def test_a_started_track_drops_its_handout_block() -> None:
    st = state(cand(1))
    st.handed["dz:1"] = 1.0

    wave._start(st, cand(1))

    assert "dz:1" not in st.handed and wave.fp_of(cand(1)) in st.heard


def test_nothing_left_but_a_flipped_past_track_is_no_batch() -> None:
    """A repeat is what the wave is fixing: when only the tracks just flipped past are left, the answer is empty
    and the client asks again later (the generators are refilling the pool meanwhile)."""
    import time as clock

    only = cand(1, score=1)
    st = state(only)
    st.handed[only.track.ref] = clock.time()

    assert wave.pick(st, profile(), "discover", {}, n=5) == []


def test_a_track_no_source_has_is_not_picked_again() -> None:
    """A 30 s preview is no wave track: the client names it, and neither the queue nor the pool offers it again."""
    st = state(cand(1, score=10), cand(2, score=1))
    st.nofile.add("dz:1")

    got = wave.pick(st, profile(), "discover", {}, n=5)

    assert [c.track.ref for c in got] == ["dz:2"]


def test_pick_filters_persisted_exclusions_and_relaxes_only_shown() -> None:
    st = state(*(cand(i) for i in range(1, 9)))
    st.exclusion_refs = {f"dz:{i}" for i in range(1, 5)}
    st.nofile = {"dz:1"}
    st.exclusion_fps = {wave.fp_of(cand(1))}
    got = wave.pick(st, profile(), "discover", {}, n=5)
    assert "dz:1" not in [c.track.ref for c in got]
    assert len(got) == 5  # shown is relaxed to top up a short pool


async def test_wave_exclusions_cross_sessions_and_expire(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import WaveExclusion

    client, uid = env["client"], env["admin"]
    await history(uid)
    pool = [tr(5000 + i) for i in range(12)]
    fake_sources(monkeypatch, pool)
    first = (await client.post("/api/wave/start", json={})).json()
    await wave.settle(first["session_id"])
    first_refs = [t["ref"] for t in first["tracks"]]
    first_refs = [t["ref"] for t in (await client.post(
        f"/api/wave/{first['session_id']}/next", json={"queued": first_refs})).json()["tracks"]]
    async with session_scope() as s:
        s.add(WaveExclusion(user_id=uid, ref=first_refs[0], fingerprint=None,
                            reason="no_file", created_at=wave.utcnow() - timedelta(days=2),
                            expires_at=wave.utcnow() + timedelta(hours=1)))
    second = (await client.post("/api/wave/start", json={})).json()
    await wave.settle(second["session_id"])
    second_refs = [t["ref"] for t in second["tracks"]]
    assert second_refs and first_refs[0] not in second_refs
    async with session_scope() as s:
        row = (await s.execute(select(WaveExclusion).where(WaveExclusion.user_id == uid,
                                                            WaveExclusion.ref == first_refs[1],
                                                            WaveExclusion.reason == "shown"))).scalar_one()
        row.expires_at = wave.utcnow() - timedelta(seconds=1)
    third = (await client.post("/api/wave/start", json={})).json()
    assert third["tracks"] and first_refs[1] not in [t["ref"] for t in third["tracks"]]


async def test_wave_probe_deadline_falls_back_for_hanging_choices(env, monkeypatch) -> None:
    import asyncio
    import time

    from app import listen

    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5100 + i) for i in range(8)])
    monkeypatch.setattr(wave, "PROBE_BATCH_TIMEOUT_S", 0.08)
    monkeypatch.setattr(wave, "FIRST_PROBE_TIMEOUT_S", 0.08)
    cancelled = asyncio.Event()

    async def choose(ref, **kwargs):
        if ref == "dz:5100":
            return listen.Choice(ref, "preview", "Artist", "Title")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def empty_vectors(rt):
        return {}

    monkeypatch.setattr(listen, "choose", choose)
    monkeypatch.setattr(wave, "_vectors", empty_vectors)
    started = time.monotonic()
    response = await env["client"].post("/api/wave/start", json={})
    elapsed = time.monotonic() - started
    refs = [t["ref"] for t in response.json()["tracks"]]

    assert response.status_code == 200
    assert elapsed < 1.0
    assert "dz:5100" not in refs
    assert not refs
    assert cancelled.is_set()
    rt = wave._rt[response.json()["session_id"]]
    assert rt.probe_cooldown.keys() & rt.state.pool.keys()  # retry later, not handed out or removed


async def test_start_hands_out_library_track_while_catalog_source_hangs(env, monkeypatch) -> None:
    import asyncio
    import time

    from app import listen
    from tests.test_mixes import add_track

    await history(env["admin"])
    track_id = await add_track(8800)
    local = CatalogTrack(ref="nd:nd8800", source="nd", id="nd8800", title="Песня 8800",
                         artist="Свой 8800", artists=["Свой 8800"], duration_s=200)
    finished = asyncio.Event()

    async def slow_catalog():
        await asyncio.Event().wait()

    async def gens(*_args):
        return [local]

    monkeypatch.setattr(wave, "generators", lambda *_args: {"local": gens, "catalog": slow_catalog})

    async def choose(*_args, **_kwargs):
        raise AssertionError("a Navidrome library file must not be probed")

    monkeypatch.setattr(listen, "choose", choose)
    started = time.monotonic()
    response = await env["client"].post("/api/wave/start", json={})
    elapsed = time.monotonic() - started
    finished.set()

    assert response.status_code == 200, response.text
    assert elapsed < 3.0
    tracks = response.json()["tracks"]
    assert len(tracks) == 1 and tracks[0]["ref"] == "nd:nd8800"
    assert wave._rt[response.json()["session_id"]].state.out["nd:nd8800"].library_track_id == track_id


async def test_preview_only_candidates_are_filtered_before_handout(env, monkeypatch) -> None:
    from app import listen

    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(8)])
    async def choose(ref, **kwargs):
        if ref == "dz:5000":
            return listen.Choice(ref, "preview", "Artist", "Title")
        return listen.Choice(ref, "full", "Artist", "Title")

    monkeypatch.setattr(listen, "choose", choose)
    async def empty_vectors(rt):
        return {}
    monkeypatch.setattr(wave, "_vectors", empty_vectors)
    result = (await env["client"].post("/api/wave/start", json={})).json()
    assert "dz:5000" not in [t["ref"] for t in result["tracks"]]
    assert len(result["tracks"]) >= 1


async def test_an_off_filter_deezer_candidate_is_not_probed(env, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from app import listen

    await history(env["admin"])
    track = tr(8101)
    fake_sources(monkeypatch, [track])
    monkeypatch.setattr(wave, "_refill", lambda sid, rt, **kwargs: None)
    choose = AsyncMock(return_value=listen.Choice(track.ref, "preview", track.artist, track.title))
    monkeypatch.setattr(listen, "choose", choose)
    response = await env["client"].post("/api/wave/start", json={"filters": {"genres": ["rock"], "lang": "ru"}})
    assert response.status_code == 200, response.text
    assert response.json()["tracks"] == []
    assert response.json()["filter_relaxed"] == ["exhausted"]
    choose.assert_not_awaited()


async def test_a_library_candidate_skips_the_probe(env, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from app import listen

    track = cand(5100)
    track.nd_song_id = "nd5100"
    rt = wave.Runtime(user_id=env["admin"], mode="normal", state=state(track), profile=profile())
    choose = AsyncMock(side_effect=AssertionError("library track was probed"))
    monkeypatch.setattr(listen, "choose", choose)
    assert await wave._probe_full(rt, [track], deadline=wave.time.monotonic() + 1) == [track]
    choose.assert_not_awaited()


async def test_a_preview_never_reaches_the_client(env, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from app import listen

    preview, unverified, local = cand(1), cand(2), cand(3)
    local.nd_song_id = "nd3"
    st = state(preview, unverified, local)
    st.nofile.add(preview.track.ref)
    st.out[preview.track.ref] = preview
    rt = wave.Runtime(user_id=env["admin"], mode="normal", state=st, profile=profile())
    choose = AsyncMock(side_effect=AssertionError("deadline exhausted"))
    monkeypatch.setattr(listen, "choose", choose)
    monkeypatch.setattr(wave, "FIRST_PROBE_TIMEOUT_S", 0)
    monkeypatch.setattr(wave, "_save", AsyncMock())
    monkeypatch.setattr(wave, "save_items", AsyncMock())
    assert await wave._probe_full(rt, [preview, unverified, local], deadline=0) == [local]
    keep, kept, added = await wave._hand_out("test", rt, [preview.track.ref], preview, first_only=True)
    assert keep == kept == []
    assert added == [local]
    choose.assert_not_awaited()


async def test_a_timeout_does_not_write_a_no_file_exclusion(env, monkeypatch) -> None:
    import asyncio

    from app import listen
    from app.db import session_scope
    from app.models import WaveExclusion

    track = cand(5100)
    rt = wave.Runtime(user_id=env["admin"], mode="normal", state=state(track), profile=profile())

    async def choose(ref, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(listen, "choose", choose)
    now = wave.time.monotonic()
    assert await wave._probe_full(rt, [track], deadline=now + 0.05) == []
    assert rt.probe_cooldown[track.track.ref] >= now + 300
    assert track.track.ref in rt.state.pool and track.track.ref not in rt.state.nofile
    async with session_scope() as s:
        assert await s.scalar(select(WaveExclusion).where(WaveExclusion.user_id == rt.user_id,
                                                         WaveExclusion.reason == "no_file")) is None


async def test_no_file_drops_the_track_from_the_session_and_survives_a_restart(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.mixes.wave import WaveSession

    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(10)])
    w = (await c.post("/api/wave/start", json={})).json()
    sid, first = w["session_id"], w["tracks"][0]["ref"]

    r = await c.post(f"/api/wave/{sid}/no_file", json={"ref": first})

    assert r.status_code == 200 and r.json() == {"ok": True}
    async with session_scope() as s:
        row = await s.get(WaveSession, sid)
    assert first in row.state_json["nofile"] and first not in [x["track"]["ref"] for x in row.state_json["out"]]
    # the answer no longer offers it
    r = (await c.post(f"/api/wave/{sid}/next", json={"queued": []})).json()
    assert first not in [t["ref"] for t in r["tracks"]]


def test_an_old_session_gets_the_handout_block_for_its_queue() -> None:
    """A session saved before `handed` existed has no such key: its queue must get the block, else the very tracks
    it holds come back as fresh candidates (a duplicate on the next answer)."""
    import time as clock

    queue = cand(1, score=10)
    saved = wave.State()
    saved.out = {queue.track.ref: queue}
    saved.pool = {cand(2, score=1).track.ref: cand(2, score=1)}
    old = saved.dump()
    old["pool"] = [wave.cand_json(c) for c in saved.pool.values()]
    old.pop("handed")  # a session of an older build

    back = wave.State.load(old)

    assert set(back.handed) == {queue.track.ref}
    assert abs(back.handed[queue.track.ref] - clock.time()) < 60
    # the active queue is preserved by _hand_out; pick itself returns only fresh pool candidates.
    assert [c.track.ref for c in wave.pick(back, profile(), "discover", {}, n=5, queued={queue.track.ref})] == \
           ["dz:2"]
    assert [c.track.ref for c in wave.pick(back, profile(), "discover", {}, n=5, queued={"dz:2"})] == ["dz:2"]


async def test_an_empty_answer_keeps_the_queue_and_asks_again(env, monkeypatch) -> None:
    """Nothing fresh is left: the answer is empty with `pending`, the session's queue is untouched (so the client's
    next `queued` still names tracks it holds) and the pool is refilled instead of repeating the flipped ones."""
    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(5)])
    w = (await c.post("/api/wave/start", json={})).json()
    sid, refs = w["session_id"], [t["ref"] for t in w["tracks"]]
    await wave.settle(sid)
    # The one-track start was skipped; the next response comes from the remaining pool.
    r = (await c.post(f"/api/wave/{sid}/next", json={"played": [ev("start", refs[0])], "queued": []})).json()

    assert [t["ref"] for t in r["tracks"]] and not set(refs) & {t["ref"] for t in r["tracks"]}
    refs = [t["ref"] for t in r["tracks"]]
    # No fresh track remains. Known refs still produce a keep-only delta.
    r = (await c.post(f"/api/wave/{sid}/next", json={"queued": refs})).json()
    assert r["keep"] == refs and r["add"] == []
    # With no valid keep and no add, ask again without clearing the server's queue.
    r = (await c.post(f"/api/wave/{sid}/next", json={"queued": ["dz:unknown"]})).json()
    assert r["tracks"] == r["keep"] == r["add"] == [] and r["pending"]
    assert list(wave._rt[sid].state.out) == refs


def test_compact_state_round_trip_keeps_float32_vectors_and_pick() -> None:
    import json
    import struct

    st = state(cand(1), cand(2))
    st.out["dz:1"] = st.pool.pop("dz:1")
    st.space = {"model": "embed-v2", "space_id": "space-1", "dim": 3,
                "vectors": {"dz:1": [0.123456789, 1.25, -2.5], "dz:2": [0.0, 0.5, 1.0]}}
    packed = json.loads(json.dumps(st.dump()))
    back = wave.State.load(packed)

    assert back.space["model"] == "embed-v2" and back.space["space_id"] == "space-1"
    for key, vector in st.space["vectors"].items():
        assert back.space["vectors"][key] == list(struct.unpack(f"{len(vector)}f", struct.pack(f"{len(vector)}f", *vector)))
    assert [c.track.ref for c in wave.pick(back, profile(), "discover", {}, n=2)]


def test_legacy_state_loads_space_vectors_and_pool() -> None:
    old = wave.State()
    old.space = {"model": "legacy", "space_id": "old-space", "vectors": {"dz:1": [0.1, 0.2]}}
    old.pool["dz:1"] = cand(1)
    legacy = old.dump()
    legacy["pool"] = [wave.cand_json(c) for c in old.pool.values()]
    legacy["space"] = old.space
    legacy.pop("space_b64")

    back = wave.State.load(legacy)

    assert back.space == old.space and back.pool["dz:1"].track.ref == "dz:1"


def test_compact_state_json_is_smaller_than_legacy() -> None:
    import json

    st = state(*(cand(i) for i in range(1, 36)))
    st.space = {"model": "embed-v2", "space_id": "space-1", "dim": 128,
                "vectors": {f"dz:{i}": [i / 1000 + j / 10000 for j in range(128)] for i in range(1, 36)}}
    modern = st.dump()
    legacy = {**modern, "pool": [wave.cand_json(c) for c in st.pool.values()],
              "space": {**st.space, "vectors": st.space["vectors"]}}
    legacy.pop("space_b64")
    old_len = len(json.dumps(legacy, separators=(",", ":")))
    new_len = len(json.dumps(modern, separators=(",", ":")))
    assert new_len < old_len * 0.5


async def test_profile_timeout_still_hands_out_seed(env, monkeypatch) -> None:
    import asyncio

    await history(env["admin"])

    async def slow_profile(*args, **kwargs):
        await asyncio.sleep(0.1)

    monkeypatch.setattr(wave, "build_profile", slow_profile)
    monkeypatch.setattr(wave, "PROFILE_TIMEOUT_S", 0.01)
    fake_sources(monkeypatch, [tr(9850 + i) for i in range(6)])
    response = await env["client"].post("/api/wave/start", json={"seed": {"ref": "dz:100"}})

    assert response.status_code == 200, response.text
    assert response.json()["tracks"] and response.json()["tracks"][0]["ref"] == "dz:100"


def test_state_survives_its_json() -> None:
    st = state(cand(1), played=["Арт A"])
    st.out["dz:2"] = cand(2)
    st.skips, st.pos, st.heard = {"x": 1}, ["dz:1"], {"fp"}

    back = wave.State.load(st.dump())

    assert back.dump() == st.dump() and not back.pool and back.out["dz:2"].track == st.out["dz:2"].track


def test_the_pool_comes_from_the_seed_or_the_mix_generators() -> None:
    p = profile()
    seed = tr(1)

    assert set(wave.generators(p, seed, None, [])) == {"ytm_radio", "lastfm_similar", "yandex_similar", "deezer_radio"}
    assert set(wave.generators(p, None, {"ref": "dz:1", "name": "Кино"}, [])) == {"deezer_radio", "favorites",
                                                                                   "ytm_radio"}
    songs = [tr(5, "Кино")]
    assert set(wave.generators(p, None, {"ref": "dz:1", "name": "Кино", "songs": songs}, [])) == {
        "deezer_radio", "favorites", "artist_songs", "ytm_radio"}
    assert set(wave.generators(p, None, {"ref": None, "name": "Кино", "songs": songs}, [])) == {
        "artist_songs", "ytm_radio"}  # no Deezer artist: the library songs and their radios
    own = wave.generators(p, None, None, [])
    assert own.keys() == build.generators(p, []).keys() and "yandex_wave" in own  # the user's own: as a mix


def test_a_refill_takes_the_next_seeds_round_and_round() -> None:
    from app.mixes.taste import TasteTrack

    p = profile()
    for i in range(5):
        t = TasteTrack(key=f"dz:{i}", ref=f"dz:{i}", artist=f"Арт {i}", title=f"Песня {i}", score=5 - i,
                       last_event=NOW)
        p.tracks[t.key] = t
    st = wave.State(seeds_used=["dz:0"])

    rounds = [(wave.refill_generators(p, st), list(st.seeds_used)) for _ in range(3)]

    assert set(rounds[0][0]) == {"ytm_radio", "lastfm_similar"}
    assert [r[1] for r in rounds] == [["dz:0", "dz:1", "dz:2", "dz:3"], ["dz:0", "dz:1", "dz:2", "dz:3", "dz:4"],
                                      ["dz:0", "dz:1", "dz:2"]]  # three new seeds a time, then all anew


# --- learning from the events ----------------------------------------------------------------------------------------


def event(kind: str, ref: str, position: float | None = None):
    from app.models import PlayEvent

    return PlayEvent(kind=kind, ref=ref, position_s=position)


async def test_typed_subject_and_compensation_do_not_alias_a_track_in_v1_session():
    rt = wave.Runtime(user_id=1, mode="normal", state=state(), profile=profile())
    rt.state.out["dz:1"] = cand(1)
    album = event("like", "dz:1")
    album.target_type = "album"
    rollback = event("like", "dz:1")
    rollback.reverts_event_id = "failed-unlike"
    await wave.learn("s", rt, [album, rollback])
    assert not rt.state.pos and not rt.state.radios and not rt.state.played


async def test_events_start_tracks_and_early_skips_count(monkeypatch: pytest.MonkeyPatch) -> None:
    radios: list[str] = []

    async def radio_of(t):
        radios.append(t.ref)
        return []

    monkeypatch.setattr(wave, "radio_of", radio_of)
    rt = wave.Runtime(user_id=1, mode="normal", state=state(), profile=profile())
    st = rt.state
    for n, a in ((1, "Арт A"), (2, "Арт B"), (3, "Арт C")):
        st.out[f"dz:{n}"] = cand(n, a)

    await wave.learn("s", rt, [event("start", "dz:1"), event("skip", "dz:1", 12), event("start", "dz:2"),
                               event("skip", "dz:2", 95), event("like", "dz:3")])

    assert [x.ref for x in st.played] == ["dz:1", "dz:2"] and st.played[0].kind == "skip"
    assert st.skips == {artist_key("Арт A"): 1}  # left after 30 s: no penalty
    assert st.neg == ["dz:1"] and st.pos == ["dz:3"]
    assert "dz:3" in st.out  # a ♥ of a track ahead doesn't start it
    assert st.radios == radios == ["dz:3"]  # the radio of the liked one is asked


# --- the API --------------------------------------------------------------------------------------------------------


@pytest.fixture
async def env(hub_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app import listen
    from app.main import create_app
    from app.services import svc

    prefetched: list[list[str]] = []
    warmed: list[list[str]] = []
    monkeypatch.setattr(listen, "prefetch", lambda uid, refs: prefetched.append(list(refs)) or len(refs))
    monkeypatch.setattr(listen, "warm_light", lambda refs: warmed.append(list(refs)))
    nd = ND()
    app = create_app()
    async with app.router.lifespan_context(app):
        svc.deezer = FakeDeezer()
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd.handler)
            async with app_client(app) as client:
                yield {"client": client, "nd": nd, "admin": await user_id("admin"), "prefetched": prefetched, "warmed": warmed}


def fake_sources(monkeypatch: pytest.MonkeyPatch, tracks: list[CatalogTrack],
                 radio: list[CatalogTrack] = ()) -> dict[str, Any]:
    """Supply catalog tracks and a full source for their synthetic IDs unless the test overrides it."""
    from app import listen

    async def choose(ref, **kwargs):
        return listen.Choice(ref, "full", "Artist", "Title")

    monkeypatch.setattr(listen, "choose", choose)
    seen: dict[str, Any] = {"seed": [], "radio": []}

    def gens(p, seed, seed_artist, seeds, **kwargs):
        seen["seed"].append(seed.ref if seed else seed_artist["ref"] if seed_artist else None)

        async def g():
            return list(tracks)

        return {"ytm_radio": g}

    async def radio_of(t):
        seen["radio"].append(t.ref)
        return list(radio)

    monkeypatch.setattr(wave, "generators", gens)
    monkeypatch.setattr(wave, "radio_of", radio_of)
    monkeypatch.setattr(wave, "refill_generators", lambda p, st, **kwargs: {})
    return seen


def ev(kind: str, ref: str, position: float | None = None) -> dict[str, Any]:
    return {"event_id": uuid.uuid4().hex, "ts": datetime.now(UTC).isoformat(), "kind": kind, "ref": ref,
            "position_s": position, "duration_s": 200, "client": "pwa"}


async def test_legacy_relaxed_queue_is_rechecked_after_restart(env, monkeypatch):
    seed = tr(8100, "Seed Artist").model_copy(update={"genre": "Jazz"})
    strict = tr(8101).model_copy(update={"genre": "Rock"})
    unknown = tr(8102)
    wrong = tr(8103).model_copy(update={"genre": "Jazz"})
    monkeypatch.setattr(wave.svc.deezer, "track", seed)
    fake_sources(monkeypatch, [strict, wrong, unknown])
    monkeypatch.setattr(wave, "_refill", lambda sid, rt, **kwargs: None)
    client = env["client"]
    r = await client.post("/api/wave/start", json={"seed": {"ref": seed.ref}, "filters": {"genres": ["rock"]}})
    assert r.status_code == 200, r.text
    w = r.json()
    sid = w["session_id"]
    assert [t["ref"] for t in w["tracks"]] == [seed.ref]
    assert w["filter_relaxed"] == []
    await wave.settle(sid)
    rt = wave._rt[sid]
    # Reproduce a persisted queue handed out by the old, relaxing implementation.
    for track in (strict, unknown, wrong):
        rt.state.out[track.ref] = build.Cand(track, gens={"ytm_radio": 0})
        rt.state.handed[track.ref] = wave.time.time()
    await wave._save(sid, rt, force=True)
    await wave.reset()

    r = await client.post(f"/api/wave/{sid}/next", json={"queued": [seed.ref, unknown.ref, strict.ref, wrong.ref]})
    assert r.status_code == 200, r.text
    batch = r.json()
    assert batch["filters"] == w["filters"]
    assert batch["keep"] == [seed.ref, strict.ref]
    assert batch["add"] == [] and batch["filter_relaxed"] == []
    assert [t["ref"] for t in batch["tracks"]] == batch["keep"]
    assert set(wave._rt[sid].state.out) == {seed.ref, strict.ref}


@pytest.fixture
def energy_embedder(monkeypatch):
    from unittest.mock import AsyncMock

    spy = AsyncMock()
    monkeypatch.setattr(wave.embedder, "configured", lambda: True)
    monkeypatch.setattr(wave.embedder.EmbedderClient, "energies", spy)
    monkeypatch.setattr(wave, "_sound", AsyncMock())
    monkeypatch.setattr(wave, "_refill", lambda sid, rt, **kwargs: None)
    monkeypatch.setattr(wave, "_discovery_library", AsyncMock(return_value=[]))
    return spy


async def _start_energy_wave(env, monkeypatch, mode, values):
    await history(env["admin"])
    tracks = [tr(8200 + i) for i in range(len(values))]
    fake_sources(monkeypatch, tracks)
    r = await env["client"].post("/api/wave/start", json={"filters": {"energy": mode}})
    assert r.status_code == 200, r.text
    return r.json()


async def _assert_strict_energy(env, monkeypatch, energy_embedder, mode, passing, failing):
    scores = {f"dz:{8200 + i}": score for i, score in enumerate(passing + failing)}
    energy_embedder.side_effect = lambda keys: {k: scores[k] for k in keys}
    start = await _start_energy_wave(env, monkeypatch, mode, scores)
    sid = start["session_id"]
    await wave.settle(sid)
    queued = [t["ref"] for t in start["tracks"]]
    assert len(queued) == 1 and start["filter_relaxed"] == []
    r = await env["client"].post(f"/api/wave/{sid}/next", json={"queued": queued})
    assert r.status_code == 200, r.text
    assert {t["ref"] for t in r.json()["tracks"]} == set(list(scores)[:len(passing)])
    assert r.json()["filter_relaxed"] == []
    energy_embedder.assert_awaited_once_with(list(scores))


async def test_energy_high_passes_only_above_the_threshold(env, monkeypatch, energy_embedder):
    await _assert_strict_energy(env, monkeypatch, energy_embedder, "high", [0.55, 0.6, 0.7, 0.8, 0.9], [0.549, 0.2, None])


async def test_energy_low_passes_only_below_the_threshold(env, monkeypatch, energy_embedder):
    await _assert_strict_energy(env, monkeypatch, energy_embedder, "low", [0.45, 0.4, 0.3, 0.2, 0.1], [0.451, 0.8, None])


async def test_unknown_energy_is_not_fetched_twice(env, energy_embedder):
    energy_embedder.return_value = {"dz:8200": None}
    st = wave.State(filters=WaveFilters(energy="high"))
    rt = wave.Runtime(user_id=env["admin"], mode="normal", state=st, profile=profile())
    result = [build.GenResult("ytm_radio", [tr(8200)])]
    assert await wave.absorb(rt, result) == 1
    assert st.energy == {"dz:8200": None}
    st.remove("dz:8200")  # Not a pool duplicate: only the energy cache prevents a second call.
    assert await wave.absorb(rt, result) == 1
    assert "dz:8200" in st.pool_relaxed
    energy_embedder.assert_awaited_once_with(["dz:8200"])


async def test_a_dead_embedder_does_not_break_the_wave(env, monkeypatch, energy_embedder, caplog):
    energy_embedder.side_effect = wave.embedder.EmbedderDown("offline")
    out = await _start_energy_wave(env, monkeypatch, "high", [None])
    assert out["tracks"] == [] and out["pending"] is False
    assert out["filter_relaxed"] == ["exhausted"]
    assert "energy lookup failed" in caplog.text


async def test_neutral_and_unknown_energy_never_fill_a_calm_wave(env, monkeypatch, energy_embedder):
    energy_embedder.return_value = {"dz:8200": 0.5, "dz:8201": None}
    out = await _start_energy_wave(env, monkeypatch, "low", energy_embedder.return_value)
    sid = out["session_id"]
    await wave.settle(sid)
    assert out["tracks"] == [] and out["pending"] is False
    r = await env["client"].post(f"/api/wave/{sid}/next", json={})
    assert r.status_code == 200, r.text
    assert r.json()["tracks"] == [] and r.json()["pending"] is False
    assert r.json()["filter_relaxed"] == ["exhausted"]
    assert not wave._rt[sid].state.pool


def test_energy_survives_the_state_round_trip():
    st = wave.State(energy={"nd:track": 0.63, "dz:1": None})
    assert wave.State.load(st.dump()).energy == st.energy
    assert wave.State.load({}).energy == {}


async def test_lang_is_never_relaxed(env, monkeypatch):
    await history(env["admin"])
    ru = tr(8103)
    fake_sources(monkeypatch, [ru])
    client = env["client"]
    r = await client.post("/api/wave/start", json={"filters": {"lang": "foreign"}})
    assert r.status_code == 200, r.text
    w = r.json()
    rt = wave._rt[w["session_id"]]
    assert ru.ref in rt.state.pool_relaxed
    # Make source exhaustion deterministic independently of real profile seed radios.
    monkeypatch.setattr(wave, "_refill", lambda sid, rt, **kwargs: None)
    await wave.settle(w["session_id"])
    r = await client.post(f'/api/wave/{w["session_id"]}/next', json={"filters": {"lang": "ru"}})
    assert r.status_code == 200, r.text
    batch = r.json()
    assert batch["filters"]["lang"] == "foreign"
    assert batch["tracks"] == [] and batch["pending"] is False
    assert batch["filter_relaxed"] == ["exhausted"]


async def test_nothing_to_start_from(env) -> None:
    r = await env["client"].post("/api/wave/start", json={})

    assert r.status_code == 409, r.text


async def test_a_wave_hands_out_five_and_learns_from_what_was_played(env, monkeypatch) -> None:
    from app.config import get_settings
    from app.db import session_scope
    from app.library.owned import quiet
    from app.models import DownloadItem, PlayEvent

    c, me = env["client"], env["admin"]
    monkeypatch.setattr(get_settings().cfg.storage, "autodownload_listened", True)
    await history(me)
    seen = fake_sources(monkeypatch, [tr(5000 + i) for i in range(30)], radio=[tr(7000, "Из радио")])

    r = await c.post("/api/wave/start", json={})
    assert r.status_code == 200, r.text
    w = r.json()
    sid, tracks = w["session_id"], w["tracks"]
    assert w["title"] == "Моя волна" and len(tracks) == 1 and seen["seed"] == [None]
    assert env["prefetched"][-1] == [t["ref"] for t in tracks if not t["owned"]]
    assert len(env["warmed"][-1]) == len(tracks)

    first = tracks[0]
    await wave.settle(sid)
    r = await c.post(f"/api/wave/{sid}/next", json={
        "played": [ev("start", first["ref"]), ev("finish", first["ref"], 200)],
        "queued": []})
    assert r.status_code == 200, r.text
    w2 = r.json()
    refs = [t["ref"] for t in w2["tracks"]]
    assert w2["recorded"]["accepted"] == 2 and len(refs) == 5
    assert first["ref"] not in refs  # played: never again
    assert seen["radio"] == [first["ref"]] and "dz:7000" in refs  # the radio of the finished track joined the pool
    assert next(t for t in w2["tracks"] if t["ref"] == "dz:7000")["generator"] == "session_radio"
    async with session_scope() as s:
        events = list((await s.execute(select(PlayEvent).where(PlayEvent.user_id == me))).scalars())
        dl = (await s.execute(select(DownloadItem).where(DownloadItem.status == "queued"))).scalar_one()
    assert {e.context for e in events if e.ref == first["ref"]} == {f"wave:{sid}"}
    # played to its end: downloaded quietly (not in «Загрузки»)
    assert dl.query_json["meta"]["track_ref"] == first["ref"] and dl.query_json["wave"] == {"session": sid}
    assert quiet(dl.query_json)


async def test_an_event_sent_through_plays_first_gets_the_wave_context(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import PlayEvent

    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(10)])
    w = (await c.post("/api/wave/start", json={})).json()
    e = {**ev("start", w["tracks"][0]["ref"]), "context": "other"}
    assert (await c.post("/api/plays/events", json={"events": [e]})).status_code == 200

    r = await c.post(f"/api/wave/{w['session_id']}/next", json={"played": [e]})

    assert r.json()["recorded"]["duplicates"] == 1
    async with session_scope() as s:
        row = (await s.execute(select(PlayEvent).where(PlayEvent.event_id == e["event_id"]))).scalar_one()
    assert row.context == f"wave:{w['session_id']}"


async def full_wave(env, monkeypatch):
    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(30)])
    w = (await c.post("/api/wave/start", json={})).json()
    assert w["keep"] == [] and w["add"] == w["tracks"] and w["target_len"] == 5
    sid = w["session_id"]
    await wave.settle(sid)
    url = f"/api/wave/{sid}/next"
    r = await c.post(url, json={"queued": [t["ref"] for t in w["tracks"]]})
    assert r.status_code == 200, r.text
    tracks = r.json()["tracks"]
    assert len(tracks) == len({t["artist"] for t in tracks}) == 5
    return url, sid, [t["ref"] for t in tracks]


async def test_the_answer_keeps_the_queue_and_appends(env, monkeypatch) -> None:
    url, sid, refs = await full_wave(env, monkeypatch)
    st = wave._rt[sid].state
    dropped_at = st.handed[refs[1]]
    # Reverse the tail to check that the client's order wins over the server's.
    queued = refs[:2] + refs[2:][::-1]
    r = await env["client"].post(url, json={"queued": queued[2:], "played": [ev("start", refs[0])]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["keep"] == queued[2:]
    assert len(out["add"]) == out["target_len"] - len(out["keep"]) == 2
    assert [t["ref"] for t in out["tracks"][:len(out["keep"])]] == out["keep"]
    assert out["tracks"][len(out["keep"]):] == out["add"]
    assert not set(refs[:2]) & {t["ref"] for t in out["tracks"]}
    assert refs[1] in st.pool and st.handed[refs[1]] == dropped_at


async def test_a_dislike_drops_only_the_disliked_track_from_keep(env, monkeypatch) -> None:
    url, _, refs = await full_wave(env, monkeypatch)
    r = await env["client"].post(url, json={"queued": refs, "played": [ev("dislike", refs[0])]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["keep"] == refs[1:] and len(out["add"]) == 1
    assert refs[0] not in {t["ref"] for t in out["tracks"]}


async def test_kept_tracks_are_not_probed_or_counted_again(env, monkeypatch) -> None:
    url, sid, refs = await full_wave(env, monkeypatch)
    st = wave._rt[sid].state
    queued = refs[1:]
    handed = {ref: st.handed[ref] for ref in queued}
    calls = {"probe": [], "save": [], "shown": []}
    probe_full, save_items, write = wave._probe_full, wave.save_items, wave._write_exclusions

    async def probe(rt, tracks, *args, **kwargs):
        calls["probe"].extend(c.track.ref for c in tracks)
        return await probe_full(rt, tracks, *args, **kwargs)

    async def save(s, sid, uid, tracks, values):
        calls["save"].extend(c.track.ref for c in tracks)
        return await save_items(s, sid, uid, tracks, values)

    async def exclusions(uid, tracks, reason, ttl):
        if reason == "shown":
            calls["shown"].extend(c.track.ref for c in tracks)
        return await write(uid, tracks, reason, ttl)

    monkeypatch.setattr(wave, "_probe_full", probe)
    monkeypatch.setattr(wave, "save_items", save)
    monkeypatch.setattr(wave, "_write_exclusions", exclusions)
    r = await env["client"].post(url, json={"queued": queued})
    assert r.status_code == 200, r.text
    out = r.json()
    added = [t["ref"] for t in out["add"]]
    assert out["keep"] == queued and len(added) == 1
    assert calls == {"probe": added, "save": added, "shown": added}
    assert env["warmed"][-1] == added
    assert {ref: st.handed[ref] for ref in queued} == handed
    assert all(item.handed_count == 1 for item in await wave_items(sid))


async def test_an_empty_queued_falls_back_to_a_full_batch(env, monkeypatch) -> None:
    url, _, refs = await full_wave(env, monkeypatch)
    r = await env["client"].post(url, json={"queued": []})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["keep"] == [] and len(out["add"]) == 5 and out["tracks"] == out["add"]
    assert not set(refs) & {t["ref"] for t in out["add"]}


async def test_keep_drops_a_skipped_and_a_no_file_track(env, monkeypatch) -> None:
    url, sid, refs = await full_wave(env, monkeypatch)
    st = wave._rt[sid].state
    # Test a no-file fingerprint even when the candidate is still in out.
    st.no_file_fps.add(wave.fp_of(st.out[refs[1]]))
    r = await env["client"].post(url, json={"queued": refs, "played": [ev("skip", refs[0], 5)]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["keep"] == refs[2:] and len(out["add"]) == 2
    assert not set(refs[:2]) & {t["ref"] for t in out["tracks"]}


async def test_keep_matches_a_navidrome_alias_after_restart(env, monkeypatch) -> None:
    url, sid, refs = await full_wave(env, monkeypatch)
    rt = wave._rt[sid]
    first = rt.state.out[refs[0]]
    first.nd_song_id = "alias-song"
    fp = wave.fp_of(first)
    await wave._save(sid, rt, force=True)
    await wave.reset()
    queued = ["nd:alias-song", "alias-song", fp, refs[0], *refs[1:]]
    r = await env["client"].post(url, json={"queued": queued})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["keep"] == [queued[0], *refs[1:]] and out["add"] == []
    assert [t["ref"] for t in out["tracks"]] == out["keep"]
    assert out["tracks"][0]["owned"] and out["tracks"][0]["nd_song_id"] == "alias-song"
    for alias in ("alias-song", fp):
        r = await env["client"].post(url, json={"queued": [alias, *refs[1:]]})
        assert r.status_code == 200, r.text
        assert r.json()["keep"] == [alias, *refs[1:]]
        assert r.json()["add"] == []

    r = await env["client"].post(f"/api/wave/{sid}/no_file", json={"ref": fp})
    assert r.status_code == 200 and r.json()["ok"], r.text
    st = wave._rt[sid].state
    assert refs[0] not in st.out and fp in st.no_file_fps
    r = await env["client"].post(url, json={"queued": [fp, *refs[1:]]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["keep"] == refs[1:]
    assert not {fp, refs[0]} & {t["ref"] for t in out["tracks"]}


async def test_delta_pins_the_new_window(env, monkeypatch) -> None:
    from app import listen

    url, _, refs = await full_wave(env, monkeypatch)
    calls = []
    pin = listen.pin

    def spy(uid, refs):
        calls.append((uid, list(refs)))
        pin(uid, refs)

    monkeypatch.setattr(listen, "pin", spy)
    r = await env["client"].post(url, json={"queued": refs[1:]})
    assert r.status_code == 200, r.text
    out = r.json()
    window = out["keep"] + [t["ref"] for t in out["add"]]
    assert calls == [(env["admin"], window)]
    assert listen.pinned_refs() == set(window)


async def test_dislikes_during_the_wave_keep_tracks_and_artists_out(env, monkeypatch) -> None:
    c = env["client"]
    await history(env["admin"])
    pool = [tr(5000 + i, f"Арт {i % 10}") for i in range(30)]
    fake_sources(monkeypatch, pool)
    w = (await c.post("/api/wave/start", json={})).json()
    sid, tracks = w["session_id"], w["tracks"]
    seed = tracks[0]
    next_out = (await c.post(f"/api/wave/{w['session_id']}/next", json={"queued": [seed["ref"]]})).json()
    tracks = next_out["tracks"]
    bad = tracks[1]

    # «Не нравится» the artist from the track menu, the track ahead through an event of the session
    assert (await c.post("/api/dislikes", json={"artist_ref": bad["artist_ref"], "artist": bad["artist"]})
            ).status_code == 200
    r = await c.post(f"/api/wave/{sid}/next", json={"played": [ev("dislike", tracks[2]["ref"])],
                                                     "queued": [t["ref"] for t in tracks]})

    got = r.json()["tracks"]
    assert got and all(t["artist"] != bad["artist"] for t in got)
    assert tracks[2]["ref"] not in [t["ref"] for t in got]


async def test_a_wave_by_track_starts_with_it(env, monkeypatch) -> None:
    c = env["client"]
    seen = fake_sources(monkeypatch, [tr(5000 + i) for i in range(10)])

    r = await c.post("/api/wave/start", json={"seed": {"ref": "dz:100"}, "mode": "discover"})

    assert r.status_code == 200, r.text
    w = r.json()
    assert w["title"] == "Волна по треку «Группа крови»" and w["seed"]["ref"] == "dz:100"
    assert w["tracks"][0]["ref"] == "dz:100" and w["tracks"][0]["generator"] == "seed"
    assert seen["seed"] == ["dz:100"] and len(w["tracks"]) == 1  # no history needed


async def test_a_wave_by_search_file_keeps_the_file_and_continues_after_restart(env, monkeypatch) -> None:
    from app import listen
    from app.services import svc
    from tests.support import cand as file_candidate

    c = env["client"]
    file = file_candidate("serenade", "https://hitmoz.test/audio/serenade.mp3", title="Serenade").model_copy(
        update={"artist": "Travis Scott", "cover_url": "https://hitmoz.test/cover.jpg"})
    ref = "file:hitmoz:serenade"
    await listen.remember_files([file])
    # An unreleased song can have no catalog record. Its exact file still starts the wave.
    svc.deezer = None
    seen = fake_sources(monkeypatch, [tr(5000 + i) for i in range(10)])

    response = await c.post("/api/wave/start", json={"seed": {"ref": ref}})

    assert response.status_code == 200, response.text
    out = response.json()
    first = out["tracks"][0]
    assert out["seed"]["ref"] == ref and out["title"] == "Волна по треку «Serenade»"
    assert (first["ref"], first["source"], first["artist"], first["title"]) == (
        ref, "file", "Travis Scott", "Serenade")
    assert first["cover_url"] == file.cover_url and first["generator"] == "seed"
    assert seen["seed"] == [ref] and ref in env["prefetched"][-1]
    await wave.settle(out["session_id"])
    await wave.reset()
    response = await c.post(f"/api/wave/{out['session_id']}/next", json={
        "played": [ev("start", ref)], "queued": []})
    assert response.status_code == 200, response.text
    assert response.json()["recorded"]["accepted"] == 1
    assert all(t["ref"] != ref for t in response.json()["tracks"])
    assert response.json()["pending"]


async def test_a_wave_rejects_an_unknown_search_file(env) -> None:
    response = await env["client"].post("/api/wave/start", json={"seed": {"ref": "file:hitmoz:unknown"}})
    assert response.status_code == 404, response.text
    assert "повторите поиск" in response.json()["detail"]


async def test_a_wave_by_a_navidrome_artist_takes_its_songs_and_its_deezer_artist(env, monkeypatch) -> None:
    from tests.test_mixes import add_track

    c, nd = env["client"], env["nd"]
    await add_track(100)  # nd100 is Deezer's dz:100
    nd.artists["ar1"] = {"id": "ar1", "name": "Кино", "album": [{"id": "al1"}, {"id": "al2"}]}
    nd.albums["al1"] = {"id": "al1", "song": [
        {"id": "nd100", "title": "Песня 100", "artist": "Кино", "artistId": "ar1"},
        {"id": "s2", "title": "Кукушка", "artist": "Кино", "artistId": "ar1", "duration": 400},
        {"id": "s3", "title": "Чужая", "artist": "Другой", "artistId": "ar9"}]}  # a compilation's other artist
    nd.albums["al2"] = {"id": "al2", "song": [
        {"id": "s4", "title": "Вместе", "artist": "Кино feat. Гость", "artistId": "ar7",
         "artists": [{"id": "ar1"}, {"id": "ar8"}]}]}
    from app.services import svc

    async def search(q, kinds=("artist", "album", "track"), limit=10):
        from app.catalog.models import SearchResults

        return SearchResults(artists=[svc.deezer.artist("2", "Кино-2"), svc.deezer.artist("1", "КИНО")])

    monkeypatch.setattr(svc.deezer, "search", search)
    got: list[dict[str, Any]] = []
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(10)])
    fake = wave.generators
    monkeypatch.setattr(wave, "generators", lambda p, seed, artist, seeds: got.append(artist) or fake(
        p, seed, artist, seeds))

    r = await c.post("/api/wave/start", json={"seed": {"nd_artist_id": "ar1"}, "mode": "discover"})

    assert r.status_code == 200, r.text
    w = r.json()
    assert w["title"] == "Волна: Кино" and w["seed"] == {"artist_ref": "dz:1", "artist": "Кино", "nd_artist_id": "ar1"}
    assert got[0]["ref"] == "dz:1"  # the exact name among Deezer's artists
    assert {t.ref for t in got[0]["songs"]} == {"dz:100", "nd:s2", "nd:s4"}  # not the compilation's other artist
    st = wave._rt[w["session_id"]].state
    assert st.seed_artist == artist_key("Кино")
    assert (await c.post("/api/wave/start", json={"seed": {"nd_artist_id": "nope"}})).status_code == 404


async def test_artist_from_library_track_resolves_and_survives_restart(env, monkeypatch) -> None:
    c, nd = env["client"], env["nd"]
    nd.songs["song"] = {"id": "song", "artistId": "ar1"}
    nd.songs["noartist"] = {"id": "noartist"}
    nd.artists["ar1"] = {"id": "ar1", "name": "Кино", "album": []}
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(10)])
    response = await c.post("/api/wave/start", json={"seed": {"nd_song_id": "song", "by_artist": True}})
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["seed"]["nd_artist_id"] == "ar1"
    await wave.reset()
    next_response = await c.post(f"/api/wave/{out['session_id']}/next", json={})
    assert next_response.status_code == 200 and next_response.json()["seed"]["nd_artist_id"] == "ar1"
    for song in ("missing", "noartist"):
        assert (await c.post("/api/wave/start", json={"seed": {"nd_song_id": song, "by_artist": True}})).status_code == 404


async def test_a_session_outlives_a_restart_but_not_another_user(env, monkeypatch) -> None:
    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(30)])
    w = (await c.post("/api/wave/start", json={})).json()
    sid, first = w["session_id"], w["tracks"][0]["ref"]
    issued = await c.post(f"/api/wave/{sid}/next", json={"played": [ev("start", first)]})
    assert issued.status_code == 200, issued.text
    queued = [t["ref"] for t in issued.json()["tracks"]]
    assert queued

    await wave.reset()  # the Hub restarted: the session loads from its row, the profile is made again
    r = await c.post(f"/api/wave/{sid}/next", json={"queued": queued})

    assert r.status_code == 200, r.text
    assert r.json()["keep"] == queued and first not in [t["ref"] for t in r.json()["tracks"]]
    assert (await c.post("/api/wave/nope/next", json={})).status_code == 404
    from app.db import session_scope
    from app.models import User

    async with session_scope() as s:
        other = await s.get(User, await add_user("other"))
    for _ in range(2):  # from memory and from the row
        with pytest.raises(wave.WaveError):
            await wave.next_batch(other, sid, [], [])
        await wave.reset()


async def test_an_idle_session_is_over(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import WaveSession

    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(10)])
    sid = (await c.post("/api/wave/start", json={})).json()["session_id"]
    await wave.reset()
    async with session_scope() as s:
        (await s.get(WaveSession, sid)).updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)

    assert (await c.post(f"/api/wave/{sid}/next", json={})).status_code == 404


async def test_settle_clears_finished_tasks_before_their_callbacks_run(monkeypatch):
    """A completed gather may return without yielding to scheduled discard callbacks."""
    import asyncio
    from types import SimpleNamespace

    class GuardedTasks(set):
        # Bound the old synchronous busy loop so this regression fails without hanging pytest.
        checks = 0

        def __bool__(self):
            self.checks += 1
            assert self.checks <= 2, "settle keeps revisiting completed tasks without yielding"
            return len(self) > 0

    task = asyncio.get_running_loop().create_future()
    task.set_result(None)
    tasks = GuardedTasks([task])
    task.add_done_callback(tasks.discard)
    monkeypatch.setitem(wave._rt, "finished-task-test", SimpleNamespace(tasks=tasks))

    await wave.settle("finished-task-test")

    assert len(tasks) == 0


async def test_v2_wave_refreshes_profile_pins_model_and_does_not_relearn_after_restart(env, monkeypatch):
    from app.config import get_settings
    from app.db import session_scope
    from app.models import WaveSession

    cfg = get_settings().cfg.mixes
    monkeypatch.setattr(cfg, "taste_model", "v2")
    c, me = env["client"], env["admin"]
    await history(me)
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(30)])
    result = await c.post("/api/wave/start", json={})
    assert result.status_code == 200, result.text
    initial = result.json()
    sid, first = initial["session_id"], initial["tracks"][0]["ref"]
    outcome = ev("finish", first, 200)
    result = await c.post(f"/api/wave/{sid}/next", json={"played": [outcome]})
    assert result.status_code == 200, result.text
    await wave.settle(sid)
    async with session_scope() as s:
        saved = (await s.get(WaveSession, sid)).state_json
    assert saved["taste_model"] == "v2" and saved["positive_weights"] == {first: 2}
    old_profile = wave._rt[sid].profile
    # A separate menu action, not submitted through wave.next, must affect its old queued pool.
    bad = result.json()["tracks"][0]["ref"]
    assert (await c.post("/api/dislikes", json={"ref": bad})).status_code == 200
    changed = await c.post(f"/api/wave/{sid}/next", json={})
    assert changed.status_code == 200, changed.text
    assert bad not in [t["ref"] for t in changed.json()["tracks"]]
    assert wave._rt[sid].profile is not old_profile
    monkeypatch.setattr(cfg, "taste_model", "v1")
    await wave.reset()
    replay = await c.post(f"/api/wave/{sid}/next", json={"played": [outcome]})
    assert replay.status_code == 200, replay.text
    assert replay.json()["recorded"]["duplicates"] == 1
    await wave.settle(sid)
    async with session_scope() as s:
        restored = (await s.get(WaveSession, sid)).state_json
    assert restored["taste_model"] == "v2" and restored["positive_weights"] == saved["positive_weights"]


async def test_event_id_cannot_be_moved_between_wave_sessions(env, monkeypatch):
    from app.db import session_scope
    from app.models import PlayEvent

    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(30)])
    first = (await c.post("/api/wave/start", json={})).json()
    second = (await c.post("/api/wave/start", json={})).json()
    e = ev("skip", first["tracks"][0]["ref"], 10)
    assert (await c.post(f"/api/wave/{first['session_id']}/next", json={"played": [e]})).status_code == 200
    assert (await c.post(f"/api/wave/{second['session_id']}/next", json={"played": [e]})).status_code == 200
    assert not wave._rt[second["session_id"]].state.skips
    async with session_scope() as s:
        row = (await s.execute(select(PlayEvent).where(PlayEvent.event_id == e["event_id"]))).scalar_one()
    assert row.context == f"wave:{first['session_id']}"


async def test_legacy_state_bootstraps_seen_journal_without_replaying_old_skip(env, monkeypatch):
    from app.db import session_scope
    from app.models import WaveSession

    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(30)])
    first = (await c.post("/api/wave/start", json={})).json()
    sid = first["session_id"]
    e = ev("skip", first["tracks"][0]["ref"], 10)
    assert (await c.post(f"/api/wave/{sid}/next", json={"played": [e]})).status_code == 200
    await wave.settle(sid)
    before = dict(wave._rt[sid].state.skips)
    async with session_scope() as s:
        row = await s.get(WaveSession, sid)
        row.state_json = {k: v for k, v in row.state_json.items() if k not in ("seen", "version", "taste_model")}
    await wave.reset()
    again = await c.post(f"/api/wave/{sid}/next", json={"played": [e]})
    assert again.status_code == 200, again.text
    assert wave._rt[sid].state.skips == before and wave._rt[sid].state.taste_model == "v1"


# --- analytics (INSIGHTS.md 6.2–6.5): what is kept of a session, never read by the wave --------------------------


async def wave_row(sid: str):
    from app.db import session_scope
    from app.models import WaveSession

    async with session_scope() as s:
        return await s.get(WaveSession, sid)


async def wave_items(sid: str) -> list:
    from app.db import session_scope
    from app.models import WaveItem

    async with session_scope() as s:
        return list((await s.execute(select(WaveItem).where(WaveItem.session_id == sid)
                                     .order_by(WaveItem.position))).scalars())


async def test_a_start_keeps_where_and_by_what_client_it_was_started(env, monkeypatch) -> None:
    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(10)])
    body = {"mode": "discover", "origin": "home_card", "client": "pwa-app", "restart_of": "0123456789abcdef"}

    r = await c.post("/api/wave/start", json=body)
    plain = await c.post("/api/wave/start", json={})

    assert r.status_code == 200 and plain.status_code == 200, r.text
    row = await wave_row(r.json()["session_id"])
    assert (row.origin, row.client, row.restart_of, row.taste_model) == ("home_card", "pwa-app",
                                                                         "0123456789abcdef", "v1")
    assert row.request_json == body and row.next_calls == 0 and row.cleared_at is None
    old = await wave_row(plain.json()["session_id"])
    assert (old.origin, old.client, old.restart_of, old.request_json) == (None, None, None, {})


@pytest.mark.parametrize("origin", ["home card", "Home_card", "1home", "a" * 33, ""],
                         ids=["space", "capitals", "digit_first", "too_long", "empty"])
async def test_a_bad_origin_is_refused(env, origin: str) -> None:
    r = await env["client"].post("/api/wave/start", json={"origin": origin})

    assert r.status_code == 422, r.text


async def test_an_old_session_is_cleared_to_a_summary_not_deleted(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import WaveSession

    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(30)])
    old = (await c.post("/api/wave/start", json={})).json()["session_id"]
    fresh = (await c.post("/api/wave/start", json={})).json()["session_id"]
    async with session_scope() as s:
        row = await s.get(WaveSession, old)
        row.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
        row.state_json = {**row.state_json, "started": 3, "skips": {"a": 1}}

    assert (await c.post("/api/wave/start", json={})).status_code == 200  # a start clears the old ones

    cleared = await wave_row(old)
    assert cleared is not None and cleared.cleared_at is not None
    assert cleared.state_json["cleared"] is True and cleared.state_json["started"] == 3
    assert not {"pool", "out", "played", "seen", "signals"} & set(cleared.state_json)
    assert len(await wave_items(old)) == 1  # the start handout stays
    assert "pool" not in (await wave_row(fresh)).state_json and (await wave_row(fresh)).cleared_at is None
    # over, from memory as from the row; its summary still loads
    assert (await c.post(f"/api/wave/{old}/next", json={})).status_code == 404
    await wave.reset()
    assert (await c.post(f"/api/wave/{old}/next", json={})).status_code == 404
    assert wave.State.load(cleared.state_json).started == 3


async def test_handed_out_tracks_are_recorded_at_consecutive_positions(env, monkeypatch) -> None:
    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(30)])
    w = (await c.post("/api/wave/start", json={})).json()
    sid, first = w["session_id"], [t["ref"] for t in w["tracks"]]

    items = await wave_items(sid)
    assert [x.ref for x in items] == first and [x.position for x in items] == [0]
    assert all(x.handed_count == 1 and x.generator == "ytm_radio" and x.user_id == env["admin"]
               for x in items)
    assert all(x.title == f"Песня {x.ref[3:]}" and x.key == x.ref and x.gens == {"ytm_radio": int(x.ref[3:]) - 5000}
               for x in items)

    r = await c.post(f"/api/wave/{sid}/next", json={"played": [ev("skip", first[0], 5)], "queued": first[1:]})
    assert r.status_code == 200, r.text
    second = [t["ref"] for t in r.json()["tracks"]]

    items = {x.ref: x for x in await wave_items(sid)}
    assert not set(second) & set(first)
    assert items[first[0]].handed_count == 1  # played: not handed out again
    assert [items[ref].position for ref in second] == list(range(1, 1 + len(second)))
    assert (await wave_row(sid)).next_calls == 1


async def test_a_failing_analytics_write_does_not_break_the_wave(env, monkeypatch) -> None:
    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(30)])

    async def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(wave, "save_items", broken)
    r = await c.post("/api/wave/start", json={})

    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert len(r.json()["tracks"]) == 1 and len((await wave_row(sid)).state_json["out"]) == 1
    assert await wave_items(sid) == []
    nxt = await c.post(f"/api/wave/{sid}/next", json={})
    assert nxt.status_code == 200 and len(nxt.json()["tracks"]) == 5, nxt.text


async def test_launch_fields_of_the_wave_events_are_stored(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import PlayEvent

    c = env["client"]
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(10)])
    w = (await c.post("/api/wave/start", json={})).json()
    sid = w["session_id"]
    await wave.settle(sid)
    more = (await c.post(f"/api/wave/{sid}/next", json={"queued": [w["tracks"][0]["ref"]]})).json()["tracks"]
    w["tracks"].extend(more)
    e = {**ev("start", w["tracks"][0]["ref"]), "launch_id": f"wave:{sid}", "launch_origin": "wave"}

    r = await c.post(f"/api/wave/{sid}/next", json={"played": [e, {**ev("start", w["tracks"][1]["ref"]),
                                                                    "launch_origin": "Bad origin"}]})

    assert r.status_code == 200 and r.json()["recorded"]["accepted"] == 1, r.text
    async with session_scope() as s:
        row = (await s.execute(select(PlayEvent).where(PlayEvent.event_id == e["event_id"]))).scalar_one()
    assert (row.context, row.launch_id, row.launch_origin) == (f"wave:{sid}", f"wave:{sid}", "wave")
