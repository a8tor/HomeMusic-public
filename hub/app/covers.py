"""Disk cache of cover images: Navidrome art (`getCoverArt`) and the Hub's own album covers.

Without a cache every cover was fetched again: `getCoverArt` went to Navidrome over HTTP as the user, and the library
cover was re-read and re-sized by Pillow per request. A page asks for dozens of covers at once, so each image is kept
as a file under `DATA_DIR/covers`: a request answers from a fresh file, a miss fetches the source once and
writes it atomically. When the source does not answer, an old file is served instead (stale-while-error).

The key is `sha256(prefix, owner, ident, size)` — a hash, so a cover id with slashes never reaches the filesystem.
`prefix` keeps the two sources apart (`nd` / `lib`). `owner` is the Hub user id for Navidrome art: Navidrome answered
`getCoverArt` as that user, and a cover one user may open must never be served from another user's entry. `ident`
carries the revision (a file's mtime and size for the Hub's own art), so a replaced cover is a new entry, not a stale
one.

Eviction follows `app/cache_policy.py`: the cache stays under a soft budget (`storage.covers_max_mb`), the oldest files
go first, and a file older than `storage.covers_keep_s` goes whatever the budget says. The cache is only covers: it
never holds a `Track`, a playlist or a like.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from fastapi import Response

from app.cache_policy import _evict, _safe_files
from app.config import get_settings

log = logging.getLogger(__name__)

# Both routes require a session; neither reverse proxies nor another login may reuse the response.
IMMUTABLE = "private, max-age=604800, immutable"
# A file served while the source is down is older than its TTL: let the client ask again soon instead of a week later.
STALE_MAX_AGE = 300

_MEDIA_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}

_PRUNE_EVERY_S = 3600.0  # a walk over the cache folder at most once an hour, on a write
_ORPHAN_TMP_S = 3600.0  # a `.tmp` this old was left by a crash, not by a write in flight
_last_prune = 0.0


@dataclass(frozen=True)
class Entry:
    """A cover the endpoint may answer with: bytes, media type, strong ETag, and whether its file is still fresh."""

    path: Path | None
    data: bytes
    content_type: str
    etag: str
    fresh: bool


def root() -> Path:
    """`DATA_DIR/covers`, next to the other data the Hub keeps (as `DATA_DIR/assistant`)."""
    return get_settings().data_dir / "covers"


def key(*parts: object) -> str:
    """A safe file name: the parts are hashed, a cover id (`al-abc/xyz`) never reaches the filesystem."""
    digest = hashlib.sha256()
    for part in parts:
        data = str(part).encode("utf-8")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def get(cache_key: str) -> Entry | None:
    base = root() / cache_key
    return _newest(base.with_suffix(ext) for ext in _MEDIA_TYPES)


def latest(group: str) -> Entry | None:
    """Find the last usable local revision without accessing the source volume."""
    try:
        return _newest((root() / group).iterdir())
    except OSError:
        return None


def put(cache_key: str, data: bytes, ext: str) -> Entry:
    """Publish a complete image atomically; a failed write leaves previous files intact."""
    dest = (root() / cache_key).with_suffix(ext)
    content_type = _MEDIA_TYPES[ext]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(dest, data)
    except OSError:
        log.warning("cover cache write failed for %s", dest, exc_info=True)
        return Entry(None, data, content_type, _etag(data), True)
    # Old revisions and other extensions are left to eviction. Readers select the newest complete file.
    _prune_soon()
    return Entry(dest, data, content_type, _etag(data), True)


def build_response(entry: Entry, if_none_match: str | None, *, user_id: int, stale: bool = False) -> Response:
    """Return a session-scoped cover; a matching If-None-Match gives an empty 304."""
    etag = f'"{entry.etag}"'
    headers = {
        "ETag": etag, "Vary": "Cookie", "X-Cover-User": str(user_id),
        "Cache-Control": f"private, max-age={STALE_MAX_AGE}" if stale else IMMUTABLE,
    }
    if _matches(if_none_match, entry.etag):
        return Response(status_code=304, headers=headers)
    return Response(entry.data, media_type=entry.content_type, headers=headers)


def prune() -> int:
    """Delete leftovers, files past `covers_keep_s`, then the oldest files until the cache fits `covers_max_mb`.

    Soft, like the audio caches: a write in flight is never removed (`_write_atomic` renames it into place), and an
    OSError of one file does not stop the pass.
    """
    folder = root()
    if not folder.is_dir():
        return 0
    cfg = get_settings().cfg.storage
    budget = cfg.covers_max_mb * 2**20
    keep = cfg.covers_keep_s
    now = time.time()
    files, parts = _safe_files(folder)
    # `_safe_files` knows `.part` of the audio caches; a cover write in flight is `.<name>.tmp`, listed as a file
    leftovers = [*parts, *(path for path in files if path.name.endswith(".tmp"))]
    files = [path for path in files if not path.name.endswith(".tmp")]
    removed = 0
    for part in leftovers:  # an interrupted write, not a cache entry
        try:
            if now - part.stat(follow_symlinks=False).st_mtime > _ORPHAN_TMP_S and _evict(part, folder):
                removed += 1
        except OSError:
            continue
    kept: list[tuple[float, int, Path]] = []
    total = 0
    for path in files:
        try:
            stat = path.stat(follow_symlinks=False)
        except OSError:
            continue
        if keep and now - stat.st_mtime > keep and _evict(path, folder):
            removed += 1
            continue
        kept.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size
    if total > budget:
        # leave a little headroom so the next writes do not prune again straight away
        for _modified, size, path in sorted(kept):
            if total <= budget * 9 // 10:
                break
            if _evict(path, folder):
                total -= size
                removed += 1
    return removed


def _newest(paths: Iterable[Path]) -> Entry | None:
    now = time.time()
    cfg = get_settings().cfg.storage
    candidates = []
    for path in paths:
        if path.suffix not in _MEDIA_TYPES:
            continue
        try:
            if path.is_symlink():
                continue
            modified = path.stat().st_mtime_ns
        except OSError:
            continue
        age = now - modified / 1e9
        if not cfg.covers_keep_s or age <= cfg.covers_keep_s:
            candidates.append((modified, path, age))
    for _, path, age in sorted(candidates, reverse=True):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        return Entry(path, data, _MEDIA_TYPES[path.suffix], _etag(data), age <= cfg.covers_ttl_s)
    return None


def image_ext(data: bytes) -> str | None:
    """Use the image signature, not an upstream MIME label, to choose the disk format."""
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _matches(if_none_match: str | None, etag: str) -> bool:
    """`If-None-Match` is a list, the tokens may be quoted or weak (`W/"…"`); `*` matches anything."""
    if not if_none_match:
        return False
    for item in if_none_match.split(","):
        token = item.strip()
        if token.startswith("W/"):
            token = token[2:]
        if token.strip('"') in (etag, "*"):
            return True
    return False


def _write_atomic(dest: Path, data: bytes) -> None:
    """A `.tmp` beside the target, fsync, `os.replace`: a reader sees either the old file or the whole new one.

    Both paths are in the same folder, so the rename never crosses a filesystem; the name ends in `.tmp`, which is how
    `prune` tells an interrupted write from a cache entry.
    """
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass  # housekeeping will retry an orphan
        raise


def _prune_soon() -> None:
    """Prune on a write, but not more than once an hour: the folder may hold thousands of small files."""
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < _PRUNE_EVERY_S:
        return
    _last_prune = now
    try:
        prune()
    except OSError:
        log.warning("cover cache prune failed", exc_info=True)