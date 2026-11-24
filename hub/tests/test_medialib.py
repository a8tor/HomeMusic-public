"""The Navidrome library for the PWA in a browser (app/api/medialib.py, playlist pages): artists, albums,
«Понравившиеся», «Главная», covers.

Navidrome is a mock: artists «Кино» (♥, 2 albums) and «Кто-то» (1 album); album "alK" holds «Группа крови» (song
"ndK", known to the Hub as Deezer dz:100) and «Звезда» ("ndZ", only Navidrome knows it). Playlist "pl1" holds both.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import httpx
import pytest
import respx

from tests.support import app_client

OK = {"status": "ok", "version": "1.16.1"}
SONGS = [
    {"id": "ndK", "title": "Группа крови", "artist": "Кино", "artistId": "arK", "album": "Группа крови",
     "albumId": "alK", "coverArt": "al-alK", "duration": 245, "track": 1, "starred": "2026-10-01T10:00:00Z"},
    {"id": "ndZ", "title": "Звезда", "artist": "Кино", "artistId": "arK", "album": "Группа крови",
     "albumId": "alK", "coverArt": "al-alK", "duration": 220, "track": 2, "starred": "2026-10-20T10:00:00Z"},
]
ALBUMS = [
    {"id": "alK", "name": "Группа крови", "artist": "Кино", "artistId": "arK", "year": 1988, "songCount": 2,
     "duration": 465, "coverArt": "al-alK", "starred": "2026-10-02T00:00:00Z"},
    {"id": "alK2", "name": "Звезда по имени Солнце", "artist": "Кино", "artistId": "arK", "year": 1989, "songCount": 10},
]
ARTISTS = [
    {"id": "arX", "name": "Кто-то", "albumCount": 1},
    {"id": "arK", "name": "Кино", "albumCount": 2, "coverArt": "ar-arK", "starred": "2026-10-01T00:00:00Z"},
]


@pytest.fixture
async def env(hub_env):
    from app.main import create_app

    # a test sets state["down"] to make Navidrome unreachable, state["fail"][<method>] = <Subsonic error code>
    state: dict = {"calls": [], "fail": {}, "down": False}

    def nd(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        p = request.url.params
        state["calls"].append((name, dict(p)))
        if state["down"]:
            raise httpx.ConnectError("connection refused", request=request)
        if name in state["fail"]:
            error = {"code": state["fail"][name], "message": "failed"}
            return httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": error}})
        body: dict = {}
        if name == "getUser":
            body = {"user": {"adminRole": True}}
        elif name == "getArtists":
            body = {"artists": {"index": [{"name": "К", "artist": ARTISTS}]}}
        elif name == "getAlbumList2":
            body = {"albumList2": {"album": ALBUMS[:1] if p["type"] == "starred" else ALBUMS}}
        elif name == "getAlbum":
            if p["id"] != "alK":
                return httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": {"code": 70, "message": "not found"}}})
            body = {"album": {**ALBUMS[0], "song": SONGS}}
        elif name == "getArtist":
            body = {"artist": {**ARTISTS[1], "album": state.get("artist_albums", ALBUMS)}}
        elif name == "getStarred2":
            body = {"starred2": {"song": SONGS, "album": ALBUMS[:1], "artist": ARTISTS[1:]}}
        elif name == "getPlaylist":
            body = {"playlist": {"id": "pl1", "name": "Дорога", "songCount": 2, "duration": 465, "entry": SONGS}}
        elif name == "getCoverArt":
            return httpx.Response(200, content=state.get("art", b"\xff\xd8jpeg"),
                                  headers={"content-type": "image/jpeg"})
        return httpx.Response(200, json={"subsonic-response": {**OK, **body}})

    app = create_app()
    async with app.router.lifespan_context(app):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd)
            async with app_client(app) as client:
                await add_track("Кино", "Группа крови", "ndK", deezer_track_id="100")
                yield {"client": client, "state": state}


async def add_track(artist: str, title: str, nd_song_id: str, **kw) -> int:
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    async with session_scope() as s:
        t = Track(uid=str(uuid.uuid4()), path=f"{artist}/{title}.mp3", nd_song_id=nd_song_id, artist=artist,
                  title=title, duration_s=245, fingerprint=fingerprint(artist, title), **kw)
        s.add(t)
        await s.flush()
        return t.id


async def test_album_songs_are_owned_tracks_with_the_hubs_refs(env):
    r = await env["client"].get("/api/nd/albums/alK")
    assert r.status_code == 200, r.text
    a = r.json()
    assert (a["title"], a["year"], a["cover_url"]) == ("Группа крови", 1988, "/api/nd/cover/al-alK?size=300")
    k, z = a["tracks"]
    # the Hub's record gives the Deezer ref; a song only Navidrome knows plays by nd:<song>
    assert (k["ref"], k["nd_song_id"], k["owned"], k["library_track_id"] is not None) == ("dz:100", "ndK", True, True)
    assert (z["ref"], z["source"], z["nd_song_id"], z["library_track_id"]) == ("nd:ndZ", "nd", "ndZ", None)
    assert (z["nd_album_id"], z["nd_artist_id"], z["cover_url"]) == ("alK", "arK", "/api/nd/cover/al-alK?size=1000")

    assert (await env["client"].get("/api/nd/albums/nope")).status_code == 404


async def test_liked_lists_the_newest_like_first(env):
    r = await env["client"].get("/api/nd/liked")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [t["nd_song_id"] for t in d["songs"]] == ["ndZ", "ndK"]
    assert [a["id"] for a in d["albums"]] == ["alK"] and [a["name"] for a in d["artists"]] == ["Кино"]


async def test_catalog_like_is_listed_before_download_and_unlike_removes_it(env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.events import bus
    from app.models import DownloadItem, User

    c = env["client"]
    async with session_scope() as s:
        uid = (await s.execute(select(User.id).where(User.nd_username == "admin"))).scalar_one()
    queue = bus.subscribe(uid)
    try:
        target = {"ref": "dz:999", "target_data": {"title": "WHAT TO DO?", "artist": "Jackboys",
                  "duration_s": 250, "cover_url": "/cover.jpg"}}
        r = await c.post("/api/star", json={"targets": [target], "event_id": "catalog-like"})
        assert r.status_code == 200, r.text
        assert '"event": "library"' in queue.get_nowait()
        liked = (await c.get("/api/nd/liked")).json()["songs"]
        assert liked[0]["ref"] == "dz:999"
        assert (liked[0]["title"], liked[0]["artist"], liked[0]["cover_url"]) == ("WHAT TO DO?", "Jackboys", "/cover.jpg")
        assert not liked[0]["owned"] and liked[0]["nd_song_id"] is None
        async with session_scope() as s:
            assert not (await s.execute(select(DownloadItem))).first()
        r = await c.post("/api/star", json={"targets": [target], "unstar": True, "event_id": "catalog-unlike"})
        assert r.status_code == 200, r.text
        assert "dz:999" not in [t["ref"] for t in (await c.get("/api/nd/liked")).json()["songs"]]
    finally:
        bus.unsubscribe(uid, queue)


@pytest.mark.parametrize("cache_key", ["listen:dz:999", "mix:track:dz:999", "dz:track:999", None])
async def test_old_catalog_like_uses_stored_metadata_without_network(env, cache_key):
    from app.catalog.cache import cache_set
    if cache_key:
        await cache_set(cache_key, {"title": "WHAT TO DO?", "artist": "Jackboys", "duration_s": 250}, 3600)
    c = env["client"]
    assert (await c.post("/api/star", json={"targets": [{"ref": "dz:999"}]})).status_code == 200
    song = (await c.get("/api/nd/liked")).json()["songs"][0]
    assert song["ref"] == "dz:999"
    assert (song["title"], song["artist"]) == (("WHAT TO DO?", "Jackboys") if cache_key else ("dz:999", ""))


async def test_own_likes_deduplicate_library_aliases_and_override_stale_nd_unlikes(env):
    c = env["client"]
    target = {"ref": "dz:100"}
    assert (await c.post("/api/star", json={"targets": [target]})).status_code == 200
    songs = (await c.get("/api/nd/liked")).json()["songs"]
    assert [t["ref"] for t in songs] == ["dz:100", "nd:ndZ"]
    assert (await c.post("/api/star", json={"targets": [target], "unstar": True})).status_code == 200
    # This fake ND still returns ndK: the explicit decision must win immediately.
    assert [t["ref"] for t in (await c.get("/api/nd/liked")).json()["songs"]] == ["nd:ndZ"]


async def test_pending_import_likes_are_visible_only_to_their_user_and_own_unlike_wins(env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Import, ImportCollection, ImportItem, User

    async with session_scope() as s:
        uid = (await s.execute(select(User.id).where(User.nd_username == "admin"))).scalar_one()
        other = User(nd_username="other", nd_password_enc="unused")
        s.add(other)
        await s.flush()
        for user_id, ref in ((uid, "dz:998"), (other.id, "dz:997")):
            imp = Import(user_id=user_id, source="yandex", method="profile", status="done")
            s.add(imp)
            await s.flush()
            coll = ImportCollection(import_id=imp.id, kind="likes", external_id="likes", title="Likes")
            s.add(coll)
            await s.flush()
            s.add(ImportItem(import_id=imp.id, collection_id=coll.id, external_id=ref, ref=ref, status="waiting",
                             raw_json={"title": "Imported song", "artist": "Artist"}))
    c = env["client"]
    songs = (await c.get("/api/nd/liked")).json()["songs"]
    assert "dz:998" in [t["ref"] for t in songs] and "dz:997" not in [t["ref"] for t in songs]
    assert (await c.post("/api/star", json={"targets": [{"ref": "dz:998"}], "unstar": True})).status_code == 200
    assert "dz:998" not in [t["ref"] for t in (await c.get("/api/nd/liked")).json()["songs"]]


async def test_home_rows_and_radio_with_liked_artists_first(env):
    r = await env["client"].get("/api/nd/home")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [a["id"] for a in d["recent"]] == ["alK", "alK2"] and [a["id"] for a in d["starred"]] == ["alK"]
    assert [(a["name"], a["starred"]) for a in d["radio"]] == [("Кино", True), ("Кто-то", False)]
    types = sorted(p["type"] for name, p in env["state"]["calls"] if name == "getAlbumList2")
    assert types == ["frequent", "newest", "recent", "starred"]


async def test_artists_artist_page_and_album_lists(env):
    c = env["client"]
    assert [a["id"] for a in (await c.get("/api/nd/artists")).json()] == ["arX", "arK"]
    page = (await c.get("/api/nd/artists/arK")).json()
    assert page["artist"]["name"] == "Кино" and [a["year"] for a in page["albums"]] == [1989, 1988]
    r = await c.get("/api/nd/albums", params={"type": "newest", "offset": 50, "size": 25})
    assert r.status_code == 200 and len(r.json()) == 2
    assert ("getAlbumList2", {"type": "newest", "size": "25", "offset": "50"}) in [
        (n, {k: p[k] for k in ("type", "size", "offset")}) for n, p in env["state"]["calls"] if n == "getAlbumList2"]
    assert (await c.get("/api/nd/albums", params={"type": "bogus"})).status_code == 422


async def test_artist_albums_newest_year_first_then_without_a_year(env):
    env["state"]["artist_albums"] = [
        {"id": "a1", "name": "Без года"}, {"id": "a2", "name": "Б", "year": 1988}, {"id": "a3", "name": "а", "year": 1988},
        {"id": "a4", "name": "Новый", "year": 1990}, {"id": "a5", "name": "Ещё без года", "year": 0},
    ]
    page = (await env["client"].get("/api/nd/artists/arK")).json()
    assert [a["id"] for a in page["albums"]] == ["a4", "a3", "a2", "a1", "a5"]


async def test_playlist_page_remove_and_delete(env):
    c = env["client"]
    r = await c.get("/api/playlists/pl1")
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["name"], p["song_count"]) == ("Дорога", 2) and [t["ref"] for t in p["tracks"]] == ["dz:100", "nd:ndZ"]

    assert (await c.post("/api/playlists/pl1/remove", json={"indexes": [1, 1]})).json() == {"ok": True}
    upd = [p for n, p in env["state"]["calls"] if n == "updatePlaylist"]
    assert upd[-1]["playlistId"] == "pl1" and upd[-1]["songIndexToRemove"] == "1"
    assert (await c.post("/api/playlists/pl1/remove", json={"indexes": []})).status_code == 422

    assert (await c.delete("/api/playlists/pl1")).json() == {"ok": True}
    assert ("deletePlaylist", "pl1") in [(n, p.get("id")) for n, p in env["state"]["calls"]]


async def test_playlist_errors_map_navidromes_codes(env):
    c, fail = env["client"], env["state"]["fail"]
    fail["getPlaylist"] = 70
    assert (await c.get("/api/playlists/gone")).status_code == 404
    fail["updatePlaylist"] = 50  # not the owner
    assert (await c.post("/api/playlists/pl1/remove", json={"indexes": [0]})).status_code == 403
    fail["deletePlaylist"] = 0  # anything else Navidrome says
    r = await c.delete("/api/playlists/pl1")
    assert r.status_code == 502 and r.json()["detail"].startswith("Navidrome")


async def test_navidrome_unreachable_is_502(env):
    env["state"]["down"] = True
    c = env["client"]
    assert (await c.get("/api/nd/home")).status_code == 502
    assert (await c.get("/api/nd/cover/al-alK")).status_code == 502
    assert (await c.get("/api/playlists/pl1")).status_code == 502


async def test_cover_is_proxied_as_the_user(env):
    r = await env["client"].get("/api/nd/cover/al-alK", params={"size": 300})
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg" and r.headers["content-type"] == "image/jpeg"
    p = [p for n, p in env["state"]["calls"] if n == "getCoverArt"][-1]
    assert (p["id"], p["size"], p["u"]) == ("al-alK", "300", "admin")


# --- the disk cache of covers (`app/covers.py`) ---------------------------------------------------------------


def _cover_calls(env) -> list[dict]:
    return [p for n, p in env["state"]["calls"] if n == "getCoverArt"]


def _cached_files() -> list[Path]:
    from app import covers

    root = covers.root()
    return sorted(p for p in root.iterdir() if p.is_file()) if root.is_dir() else []


async def test_cover_is_fetched_once_then_served_from_disk(env):
    c = env["client"]
    first = await c.get("/api/nd/cover/al-alK", params={"size": 300})
    assert first.status_code == 200
    assert len(_cover_calls(env)) == 1
    second = await c.get("/api/nd/cover/al-alK", params={"size": 300})
    assert second.status_code == 200 and second.content == first.content
    assert len(_cover_calls(env)) == 1, "the second request must not ask Navidrome again"
    # a different size is a different entry: it does go to the source once
    assert (await c.get("/api/nd/cover/al-alK", params={"size": 600})).status_code == 200
    assert len(_cover_calls(env)) == 2
    assert len(_cached_files()) == 2


async def test_cover_headers_scope_caching_to_session_and_answer_304(env):
    c = env["client"]
    first = await c.get("/api/nd/cover/al-alK", params={"size": 300})
    assert first.headers["cache-control"] == "private, max-age=604800, immutable"
    assert first.headers["vary"] == "Cookie"
    assert first.headers["x-cover-user"] == str((await c.get("/api/auth/me")).json()["id"])
    etag = first.headers["etag"]
    assert etag == f'"{hashlib.sha256(first.content).hexdigest()}"'
    again = await c.get("/api/nd/cover/al-alK", params={"size": 300}, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == etag
    # a weak or unquoted token from an old client matches too
    assert (await c.get("/api/nd/cover/al-alK", params={"size": 300},
                        headers={"If-None-Match": f"W/{etag}"})).status_code == 304


async def test_cover_cache_file_is_written_atomically(env, monkeypatch):
    from app import covers

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(covers.os, "replace", spy)
    r = await env["client"].get("/api/nd/cover/al-alK", params={"size": 300})
    assert r.status_code == 200
    assert len(calls) == 1, "the cache entry must be renamed into place, not written in place"
    src, dst = calls[0]
    assert src.endswith(".tmp") and dst.endswith(".jpg")
    # no half-written leftovers, and the file holds exactly the image that was served
    files = _cached_files()
    assert len(files) == 1 and files[0].read_bytes() == r.content
    assert len(files[0].stem) == 64
    assert not [p for p in covers.root().iterdir() if p.name.endswith(".tmp")]


async def test_cover_uses_image_signature_and_preserves_old_file_on_write_failure(env, monkeypatch):
    from app import covers

    c = env["client"]
    url = "/api/nd/cover/al-alK"
    first = await c.get(url)
    old = _cached_files()[0]
    monkeypatch.setattr(covers.get_settings().cfg.storage, "covers_ttl_s", 0)
    env["state"]["art"] = b"\x89PNG\r\n\x1a\nexample"

    def fail_replace(src, dst):
        raise OSError("cache temporarily unwritable")

    with monkeypatch.context() as patch:
        patch.setattr(covers.os, "replace", fail_replace)
        r = await c.get(url)
    assert r.headers["content-type"] == "image/png"
    assert _cached_files() == [old] and old.read_bytes() == first.content

    r = await c.get(url)
    assert r.headers["content-type"] == "image/png"
    assert old.with_suffix(".png").read_bytes() == r.content


async def test_expired_cover_is_served_when_the_source_is_down(env):
    from app import covers

    c = env["client"]
    first = await c.get("/api/nd/cover/al-alK", params={"size": 300})
    assert first.status_code == 200
    path = _cached_files()[0]
    old = path.stat().st_mtime - (covers.get_settings().cfg.storage.covers_ttl_s + 60)
    os.utime(path, (old, old))
    env["state"]["down"] = True
    stale = await c.get("/api/nd/cover/al-alK", params={"size": 300})
    assert stale.status_code == 200 and stale.content == first.content
    assert stale.headers["cache-control"] == "private, max-age=300", "a stale image must be rechecked soon"
    assert stale.headers["etag"] == first.headers["etag"]


async def test_unknown_cover_is_404_and_nothing_is_cached(env):
    env["state"]["fail"]["getCoverArt"] = 70
    r = await env["client"].get("/api/nd/cover/no-such-cover", params={"size": 300})
    assert r.status_code == 404
    assert _cached_files() == []
    # and the failure is not remembered: the same id works once Navidrome knows it
    env["state"]["fail"].clear()
    assert (await env["client"].get("/api/nd/cover/no-such-cover", params={"size": 300})).status_code == 200


async def test_cover_cache_prunes_to_the_budget_and_drops_orphan_tmp(env, monkeypatch):
    from app import covers
    from app.config import get_settings

    folder = covers.root()
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        (folder / f"{i:040d}.jpg").write_bytes(b"x" * 100)
    orphan = folder / ".dead.jpg.0123456789abcdef.tmp"
    orphan.write_bytes(b"y" * 100)
    old = 1.0  # older than `covers_keep_s` in the test settings
    os.utime(orphan, (old, old))
    monkeypatch.setattr(get_settings().cfg.storage, "covers_max_mb", 0)  # budget 0: everything goes
    removed = covers.prune()
    assert removed == 5
    assert list(folder.iterdir()) == []


async def test_library_cover_is_resized_once_and_cached(env, monkeypatch):
    from PIL import Image
    from sqlalchemy import select

    from app import covers
    from app.api import library as library_api
    from app.config import get_settings
    from app.db import session_scope
    from app.models import Album

    # the layout of `app.library.importer.album_reldir`: `{Artist}/{Year} - {Album}`
    album_dir = get_settings().library_dir / "Кино" / "1988 - Группа крови"
    album_dir.mkdir(parents=True)
    Image.new("RGB", (400, 400), (10, 20, 30)).save(album_dir / "cover.jpg", "JPEG")
    calls: list[int] = []
    real_resize = library_api._resize

    def spy(data: bytes, size: int) -> bytes:
        calls.append(size)
        return real_resize(data, size)

    monkeypatch.setattr(library_api, "_resize", spy)
    async with session_scope() as s:
        s.add(Album(uid=str(uuid.uuid4()), title="Группа крови", album_artist="Кино", year=1988))
        await s.commit()
    async with session_scope() as s:
        album_id = (await s.execute(select(Album.id))).scalar_one()

    c = env["client"]
    album_url = f"/api/library/albums/{album_id}"
    url = f"{album_url}/cover?size=300"
    source = album_dir / "cover.jpg"
    assert (await c.get(album_url)).json()["cover_rev"] == str(source.stat().st_mtime_ns)
    first = await c.get(url)
    assert first.status_code == 200 and first.headers["content-type"] == "image/jpeg"
    assert first.headers["cache-control"] == "private, max-age=604800, immutable"
    second = await c.get(url)
    assert second.status_code == 200 and second.content == first.content
    assert calls == [300], "Pillow must resize once, then the disk cache answers"
    assert (await c.get(url, headers={"If-None-Match": first.headers["etag"]})).status_code == 304
    assert (await c.get("/api/library/albums/999/cover")).status_code == 404

    cached, = covers.root().glob("*/*.jpg")
    old = cached.stat().st_mtime - (get_settings().cfg.storage.covers_ttl_s + 60)
    os.utime(cached, (old, old))
    assert (await c.get(url)).content == first.content
    assert calls == [300, 300], "an expired entry must be refreshed when the source is available"

    Image.new("RGB", (420, 420), (100, 120, 130)).save(source, "JPEG")
    updated = await c.get(url, headers={"If-None-Match": first.headers["etag"]})
    assert updated.status_code == 200 and updated.headers["etag"] != first.headers["etag"]
    files = list(covers.root().glob("*/*.jpg"))
    assert len(files) == 2
    for path in files:
        os.utime(path, (path.stat().st_mtime - 7 * 86400 - 60,) * 2)
    source.unlink()
    stale = await c.get(url)
    assert stale.status_code == 200 and stale.content == updated.content
    assert stale.headers["cache-control"] == "private, max-age=300"
    assert (await c.get(f"{album_url}/cover?size=600")).status_code == 404

    nd_url = "/api/nd/cover/al-alK?size=300"
    assert (await c.get(nd_url)).status_code == 200
    assert (await c.post("/api/auth/logout")).status_code == 200
    for cover_url in (url, nd_url):
        assert (await c.get(cover_url, headers={"If-None-Match": "*"})).status_code == 401


async def test_library_needs_a_session(hub_env):
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with app_client(app, login=None) as client:
            assert (await client.get("/api/nd/home")).status_code == 401
