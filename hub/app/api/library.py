"""Own library: albums, tracks, covers, stars (FR-4 «В коллекцию», FR-5 editor)."""

from __future__ import annotations

import asyncio
import io
import stat
import uuid
from os import stat_result
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import covers
from app.api.schemas import (
    AlbumCreateIn,
    AlbumMergeIn,
    AlbumPatchIn,
    LibAlbumOut,
    LibraryPolicyOut,
    LibTrackOut,
    StarIn,
    StarredOut,
    TrackPatchIn,
    TracksDeletedOut,
    TracksMoveIn,
    TrackSourceOut,
)
from app.auth import admin_user, current_user, user_navidrome
from app.catalog.models import split_ref
from app.config import get_settings
from app.db import get_session
from app.events import bus
from app.jobs.downloads import enqueue_scan
from app.jobs.queue import notify
from app.library import editor, tagger
from app.library.importer import album_reldir, library_abs
from app.matching.normalize import normalize_title
from app.models import Album, Track, User

router = APIRouter(tags=["library"])


def track_out(t: Track) -> LibTrackOut:
    return LibTrackOut(
        id=t.id, nd_song_id=t.nd_song_id, album_id=t.album_id, artist=t.artist, title=t.title, album=t.album,
        album_artist=t.album_artist, track_no=t.track_no, disc_no=t.disc_no, year=t.year, duration_s=t.duration_s,
        bitrate_kbps=t.bitrate_kbps, codec=t.codec, explicit=t.explicit, source=t.source, locked=t.locked,
        warnings=t.warnings or [], path=t.path,
    )


def _cover_rev(a: Album) -> str | None:
    """Keep the nanosecond timestamp exact when the client parses the JSON."""
    try:
        st = (library_abs(album_reldir(a)) / "cover.jpg").stat()
    except OSError:
        return None
    return str(st.st_mtime_ns) if stat.S_ISREG(st.st_mode) else None


async def album_out(s: AsyncSession, a: Album, *, with_tracks: bool = False) -> LibAlbumOut:
    tracks = (
        await s.execute(select(Track).where(Track.album_id == a.id).order_by(Track.disc_no, Track.track_no, Track.id))
    ).scalars().all()
    rev = _cover_rev(a)
    return LibAlbumOut(
        id=a.id, uid=a.uid, title=a.title, album_artist=a.album_artist, year=a.year, release_type=a.release_type,
        genre=a.genre, deezer_album_id=a.deezer_album_id, track_count=len(tracks), has_cover=rev is not None,
        cover_rev=rev, tracks=[track_out(t) for t in tracks] if with_tracks else None,
    )


async def _album(s: AsyncSession, album_id: int) -> Album:
    a = await s.get(Album, album_id)
    if a is None:
        raise HTTPException(404, "Альбом не найден")
    return a


async def _after_edit(s: AsyncSession, user: User, album_id: int | None) -> None:
    await enqueue_scan(s)
    await s.commit()
    notify()
    bus.publish(user.id, "library", {"kind": "changed", "album_id": album_id})


def _may_delete(user: User, track: Track) -> bool:
    """Who may send a track to the trash: an admin, or the one who added the file. Tracks with no
    author (`added_by IS NULL`, files just dropped into the library and picked up by the scan) are
    admin-only. Callers turn `False` into a 403."""
    return user.is_admin or (track.added_by is not None and track.added_by == user.id)


@router.get("/api/library/policy", response_model=LibraryPolicyOut)
async def library_policy(user: User = Depends(current_user)):
    """The retention of deleted files the UI shows in its delete confirmations (no hardcoded 30 days)."""
    return LibraryPolicyOut(trash_days=get_settings().cfg.library.trash_days)


@router.get("/api/library/albums", response_model=list[LibAlbumOut])
async def list_albums(q: str | None = None, offset: int = 0, limit: int = Query(50, le=200),
                      user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    rows = (await s.execute(select(Album).order_by(Album.updated_at.desc()))).scalars().all()
    if q:
        nq = normalize_title(q).text
        rows = [a for a in rows if all(w in normalize_title(f"{a.album_artist} {a.title}").text for w in nq.split())]
    rows = rows[offset: offset + limit]
    counts = dict((await s.execute(
        select(Track.album_id, func.count(Track.id)).where(Track.album_id.in_([a.id for a in rows]))
        .group_by(Track.album_id)
    )).all())
    out = []
    for a in rows:
        rev = _cover_rev(a)  # one stat per album: `has_cover` and the URL revision are the same fact
        out.append(LibAlbumOut(id=a.id, uid=a.uid, title=a.title, album_artist=a.album_artist, year=a.year,
                               release_type=a.release_type, genre=a.genre, deezer_album_id=a.deezer_album_id,
                               track_count=counts.get(a.id, 0), has_cover=rev is not None, cover_rev=rev))
    return out


@router.post("/api/library/albums", response_model=LibAlbumOut)
async def create_album(body: AlbumCreateIn, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    a = Album(uid=str(uuid.uuid4()), title=body.title.strip(), album_artist=body.album_artist.strip(), year=body.year,
              genre=body.genre, release_type=body.release_type, created_by=user.id)
    s.add(a)
    await s.flush()
    return await album_out(s, a, with_tracks=True)


@router.get("/api/library/track-source", response_model=TrackSourceOut)
async def track_source(nd_song_id: str, user: User = Depends(current_user),
                       s: AsyncSession = Depends(get_session, scope="function")):
    t = (await s.execute(select(Track).where(Track.nd_song_id == nd_song_id).limit(1))).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "Трек не найден")
    return TrackSourceOut(source=t.source, source_url=t.source_url, acquired_at=t.acquired_at)


@router.get("/api/library/albums/{album_id}", response_model=LibAlbumOut)
async def get_album(album_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    return await album_out(s, await _album(s, album_id), with_tracks=True)


@router.patch("/api/library/albums/{album_id}", response_model=LibAlbumOut)
async def patch_album(album_id: int, body: AlbumPatchIn, user: User = Depends(current_user),
                      s: AsyncSession = Depends(get_session, scope="function")):
    a = await _album(s, album_id)
    await editor.update_album(s, a, **body.model_dump(exclude_unset=True))
    await _after_edit(s, user, a.id)
    return await album_out(s, a, with_tracks=True)


@router.put("/api/library/albums/{album_id}/cover", response_model=LibAlbumOut)
async def put_cover(album_id: int, file: UploadFile = File(...), user: User = Depends(current_user),
                    s: AsyncSession = Depends(get_session, scope="function")):
    a = await _album(s, album_id)
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "Картинка слишком большая")
    try:
        await editor.set_album_cover(s, a, data)
    except Exception as e:
        raise HTTPException(400, f"Не удалось прочитать картинку: {e}") from e
    await _after_edit(s, user, a.id)
    return await album_out(s, a, with_tracks=True)


@router.get("/api/library/albums/{album_id}/cover")
async def get_cover(album_id: int, size: int | None = Query(None, ge=32, le=1000),
                    if_none_match: str | None = Header(None), user: User = Depends(current_user),
                    s: AsyncSession = Depends(get_session, scope="function")):
    """Cache each requested size while tracking the revision of the original image."""
    a = await _album(s, album_id)
    size = size or 1000
    group = covers.key("lib", a.uid, size)
    source = library_abs(album_reldir(a)) / "cover.jpg"
    try:
        st = await asyncio.to_thread(source.stat)
        if not stat.S_ISREG(st.st_mode):
            raise FileNotFoundError(source)
        embedded = False
    except FileNotFoundError:
        t = await s.scalar(select(Track).where(Track.album_id == a.id).order_by(Track.id).limit(1))
        source = library_abs(t.path) if t is not None else None
        st = None
        embedded = True
    except OSError:
        source = None
        st = None
        embedded = False

    if source is not None:
        entry = await asyncio.to_thread(_load_album_cover, group, source, size, embedded, st)
        if entry is not None:
            return covers.build_response(entry, if_none_match, user_id=user.id)

    entry = await asyncio.to_thread(covers.latest, group)
    if entry is None:
        raise HTTPException(404, "Нет обложки")
    return covers.build_response(entry, if_none_match, user_id=user.id, stale=True)


def _load_album_cover(group: str, source: Path, size: int, embedded: bool,
                      st: stat_result | None) -> covers.Entry | None:
    from PIL import Image

    try:
        st = st if st is not None else source.stat()
        if not stat.S_ISREG(st.st_mode):
            return None
        revision = covers.key(source, st.st_mtime_ns, st.st_size)
        cache_key = f"{group}/{revision}"
        entry = covers.get(cache_key)
        if entry is not None and entry.fresh:
            return entry
        data = tagger.read_cover(source) if embedded else source.read_bytes()
        if data:
            return covers.put(cache_key, _resize(data, size), ".jpg")
    except (OSError, ValueError, Image.DecompressionBombError):
        pass
    return None


def _resize(data: bytes, size: int) -> bytes:
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@router.delete("/api/library/albums/{album_id}", response_model=TracksDeletedOut)
async def delete_album(album_id: int, user: User = Depends(admin_user), s: AsyncSession = Depends(get_session, scope="function")):
    # an album has no single owner (its tracks may come from different users), so this is admin-only
    a = await _album(s, album_id)
    n = await editor.trash_album(s, a)
    await _after_edit(s, user, None)
    return TracksDeletedOut(tracks_deleted=n)


@router.post("/api/library/albums/merge", response_model=LibAlbumOut)
async def merge(body: AlbumMergeIn, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    target = await _album(s, body.target_id)
    await editor.merge_albums(s, body.source_ids, target)
    await _after_edit(s, user, target.id)
    return await album_out(s, target, with_tracks=True)


@router.post("/api/library/tracks/move", response_model=LibAlbumOut)
async def move(body: TracksMoveIn, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    target = await _album(s, body.album_id)
    await editor.move_tracks(s, body.track_ids, target)
    await _after_edit(s, user, target.id)
    return await album_out(s, target, with_tracks=True)


@router.patch("/api/library/tracks/{track_id}", response_model=LibTrackOut)
async def patch_track(track_id: int, body: TrackPatchIn, user: User = Depends(current_user),
                      s: AsyncSession = Depends(get_session, scope="function")):
    t = await s.get(Track, track_id)
    if t is None:
        raise HTTPException(404, "Трек не найден")
    await editor.update_track(s, t, **body.model_dump(exclude_unset=True))
    await _after_edit(s, user, t.album_id)
    return track_out(t)


@router.delete("/api/library/tracks/{track_id}")
async def delete_track(track_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    t = await s.get(Track, track_id)
    if t is None:
        raise HTTPException(404, "Трек не найден")
    if not _may_delete(user, t):
        raise HTTPException(403, "Этот трек добавил другой пользователь")
    album_id = t.album_id
    await editor.trash_track(s, t)
    await _after_edit(s, user, album_id)
    return {"ok": True}


@router.get("/api/starred", response_model=StarredOut)
async def starred(user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    from app.api.schemas import FavoriteTarget
    from app.favorites import ND_FIELDS, AliasIndex, imported_targets, snapshot
    from app.imports.apply import pending_like_refs
    from app.models import FavoriteSubject

    nd = user_navidrome(user)
    try:
        body = await nd.get_starred2()
    finally:
        await nd.aclose()
    states = await snapshot(s, user.id)
    subjects = [dict(target_type=x.target_type, key=x.key, ref=x.ref, nd_id=x.nd_id,
                     liked=x.liked, liked_since=x.since, target_data=x.data,
                     event_id=x.event.event_id, aliases=x.aliases, provenance="own") for x in states.values()]
    index = AliasIndex([x.event for x in states.values()],
                       list((await s.execute(select(FavoriteSubject))).scalars()))
    for state in states.values():
        index.join(state.target_type, set(state.aliases))
    imported = {index.root(k) for k in await imported_targets(s, user.id)}
    for typ, field in (("track", "song"), ("album", "album"), ("artist", "artist")):
        for obj in body.get(field, []):
            if not obj.get("id"):
                continue
            target = FavoriteTarget(target_type=typ, **{ND_FIELDS[typ]: obj["id"]})
            key = index.key(target)
            if key in imported and key not in states:
                subjects.append(dict(target_type=typ, key=key[1], nd_id=obj["id"], liked=True,
                                     aliases=[f"nd:{obj['id']}"], provenance="import"))
    refs = set(await pending_like_refs(s, user.id))
    for ref in sorted(refs):
        key = index.key(FavoriteTarget(ref=ref))
        if key not in states:
            subjects.append(dict(target_type="track", key=key[1], ref=ref, nd_id=None, liked=True,
                                 aliases=[f"ref:{ref}"], provenance="import"))
    refs.update(x.ref for x in states.values() if x.target_type == "track" and x.liked and x.ref and not x.nd_id)
    return StarredOut(song_ids=[x["id"] for x in body.get("song", []) if x.get("id")],
                      album_ids=[x["id"] for x in body.get("album", []) if x.get("id")],
                      artist_ids=[x["id"] for x in body.get("artist", []) if x.get("id")],
                      refs=sorted(refs), subjects=subjects)


@router.post("/api/star")
async def star(body: StarIn, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    from app.api.schemas import FavoriteTarget
    from app.favorites import command

    targets = list(body.targets)
    targets += [FavoriteTarget(nd_song_id=x) for x in body.song_ids]
    targets += [FavoriteTarget(target_type="album", nd_album_id=x) for x in body.album_ids]
    targets += [FavoriteTarget(target_type="artist", nd_artist_id=x) for x in body.artist_ids]
    if body.refs and not body.unstar:
        raise HTTPException(400, "Чтобы отметить трек каталога, поставьте его в загрузку с ♥")
    targets += [FavoriteTarget(ref=x) for x in body.refs]
    nd = user_navidrome(user)
    try:
        duplicate_unlike = False
        if body.unstar and body.event_id and len(targets) == 1:
            from app.models import PlayEvent
            duplicate_unlike = (await s.execute(select(PlayEvent.id).where(
                PlayEvent.user_id == user.id, PlayEvent.event_id == body.event_id))).first() is not None
        if body.catalog_album_ref:
            src, id_ = split_ref(body.catalog_album_ref)
            q = select(Track.nd_song_id).join(Album, Album.id == Track.album_id).where(Track.nd_song_id.is_not(None))
            q = q.where(Album.deezer_album_id == id_) if src == "dz" else q.where(Album.id == -1)
            song_id = (await s.execute(q.limit(1))).scalar_one_or_none()
            if song_id is None:
                raise HTTPException(409, "Этого альбома ещё нет в библиотеке — сначала скачайте его")
            song = await nd.get_song(song_id)
            if not song.get("albumId"):
                raise HTTPException(409, "Альбом ещё не появился в Navidrome")
            targets.append(FavoriteTarget(target_type="album", ref=body.catalog_album_ref,
                                          nd_album_id=song["albumId"], target_data={"artist": song.get("artist")}))
        events = await command(s, user, targets, liked=not body.unstar, operation=body.event_id,
                               ts=body.ts, client=body.client, context=body.context, nd=nd)
        # Telemetry may have stored the same decision before the command. Pending downloads
        # still need cancellation, but an older unlike must not cancel a newer like.
        if body.unstar and body.event_id:
            from app.favorites import clear_pending, snapshot
            states = await snapshot(s, user.id)
            for target in targets:
                if target.target_type != "track" or not target.ref:
                    continue
                state = next((x for x in states.values() if x.target_type == "track" and x.ref == target.ref), None)
                if state and not state.liked:
                    await clear_pending(s, user.id, target.ref)
                    if duplicate_unlike and state.nd_id:
                        await nd.unstar(song_ids=[state.nd_id])
    except Exception:
        # Preserve successful chunks of a bulk request even if a later chunk fails.
        await s.commit()
        raise
    finally:
        await nd.aclose()
    await s.commit()
    bus.publish(user.id, "library", {"kind": "unstarred" if body.unstar else "starred"})
    return {"ok": True, "event_ids": [e.event_id for e in events]}
