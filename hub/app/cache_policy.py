"""Shared soft byte budget and safe eviction for the listening audio caches.

Two folders under staging hold audio that is a cache, not the library: `listen` (server copies of picked files, and
the copies of sources without plain HTTP audio) and `listen-light` (the Opus variants). They share one soft ceiling
(`storage.listen_max_mb`) and one reserve of free space (`storage.listen_min_free_mb`): a small SSD must not be
filled by files a user merely listened to.

What may go, in order: the oldest `.part` leftovers (an interrupted download/encode), then the files with the lowest
demand, where demand is how often the ref was listened to (`catalog_cache` key `listen:<ref>`, written by
`app.listen.note_demand`) divided by their age since the later of their mtime and their last demand. Files in active
use are never touched: the caller passes the folders/files of in-flight copies, downloads and encodes.

The limit is soft: an active stream can take the cache past it, and nothing is deleted to satisfy it while a file is
being read, written or encoded. Pruning only removes cache files — never a `Track`, a playlist or a like.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from app.config import get_settings


FRESH_GRACE_S = 900.0


def _safe_files(root: Path) -> tuple[list[Path], list[Path]]:
    """Regular files under `root`, split into finished files and `.part` leftovers. Symlinks are skipped: a cache
    entry must never point outside the cache root."""
    if not root.is_dir():
        return [], []
    files: list[Path] = []
    parts: list[Path] = []
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        path = Path(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            (parts if entry.name.endswith(".part") else files).append(path)
                    except OSError:
                        continue
        except OSError:
            continue
    return files, parts


def _is_protected(path: Path, protected: set[Path]) -> bool:
    return any(path == item or item in path.parents for item in protected)


def _evict(path: Path, root: Path) -> bool:
    """Delete one cache file and the folder that held it when it is now empty.

    Every cache entry lives in its own `<hash>/` folder. Leaving the folder behind would keep the entry's name
    listed — and the next prune would walk it again — although nothing of the audio is left: the space is only
    returned once the folder goes too. `root` is the cache root the walk started from, so a folder above it is
    never removed.
    """
    try:
        path.unlink()
    except OSError:
        return False
    parent = path.parent
    try:
        if parent != root and parent.is_relative_to(root):
            parent.rmdir()  # only when empty: a second file of that entry keeps the folder
    except (OSError, ValueError):
        pass
    return True


def prune_roots(roots: Iterable[Path], protected: set[Path] | None = None, *,
                fresh_grace_s: float = FRESH_GRACE_S) -> None:
    """Evict cache files until the combined size fits the soft budget and the free-space reserve is back.

    `protected`: files or whole folders that must stay (a copy being downloaded, a stream reading it, an encode).
    """
    roots = [r.resolve() for r in roots if r.exists()]
    if not roots:
        return
    protected_paths = {p.resolve(strict=False) for p in (protected or set())}
    files: list[tuple[float, int, Path, Path]] = []
    total = 0
    now = time.time()
    for root in roots:
        candidates, parts = _safe_files(root)
        for part in parts:
            try:
                if _is_protected(part.resolve(strict=False), protected_paths):
                    continue  # an encode still writing it
                if now - part.stat(follow_symlinks=False).st_mtime > 3600:
                    part.unlink(missing_ok=True)
            except OSError:
                pass
        for path in candidates:
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    continue  # a symlinked parent escaped the cache root
                stat = path.stat(follow_symlinks=False)
            except (OSError, ValueError):
                continue
            total += stat.st_size
            if not _is_protected(resolved, protected_paths):
                files.append((stat.st_mtime, stat.st_size, path, resolved))
    cfg = get_settings().cfg.storage
    budget = cfg.listen_max_mb * 2**20
    min_free = cfg.listen_min_free_mb * 2**20
    cutoff = now - cfg.listen_keep_s
    try:
        free = shutil.disk_usage(get_settings().staging_dir).free
    except OSError:
        free = min_free

    demand_by_dir = {hashlib.sha1(ref.encode()).hexdigest()[:16]: value
                     for ref, value in _demand_snapshot().items()}

    def retention(row: tuple[float, int, Path, Path]) -> tuple[float, float, int]:
        modified, size, path, _ = row
        # A ref's directory name is a hash of the ref: stable across a renewed stream URL and a Hub restart.
        count, at = demand_by_dir.get(path.parent.name, (0, 0.0))
        if now - modified < fresh_grace_s:
            count = max(count, 1)
        age = max(0.0, (now - max(modified, at)) / 86400.0)
        # Least retention first: the lowest demand per day of age goes first, and among equals the older file
        return (count / (1.0 + age), modified, size)

    ordered = sorted(files, key=retention)

    def root_of(path: Path) -> Path:
        return next((r for r in roots if path.is_relative_to(r)), roots[0])

    # An old file always goes, whatever the budget says: keeping it only spends the SSD on something nobody played
    # for `listen_keep_s`. This has to be its own pass — a very popular old file sorts late by retention, and the
    # budget pass below stops at the first fresh file it meets, so a single loop would leave it behind.
    for row in ordered:
        modified, size, path, _ = row
        if modified >= cutoff:
            continue
        if _evict(path, root_of(path)):
            total -= size
            free += size
    # A fresh file goes only when the cache is over its soft budget or the disk is short of the reserve.
    for row in ordered:
        modified, size, path, _ = row
        if total <= budget and free >= min_free:
            break
        if not path.exists():
            continue  # already evicted by the expiry pass
        if _evict(path, root_of(path)):
            total -= size
            free += size


def _demand_snapshot() -> dict[str, tuple[int, float]]:
    """`ref -> (times listened, last listen)`, read straight from the DB so a background prune needs no session."""
    try:
        db = get_settings().db_path
        if not db.is_file():
            return {}
        with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1) as conn:
            rows = conn.execute("SELECT key, payload_json FROM catalog_cache WHERE key LIKE 'listen:%'").fetchall()
        out: dict[str, tuple[int, float]] = {}
        for key, raw in rows:
            try:
                data = json.loads(raw).get("v") or {}
            except (TypeError, ValueError):
                continue
            demand = data.get("demand") or {}
            at = demand.get("at")
            try:
                stamp = (datetime.datetime.fromisoformat(at).replace(tzinfo=datetime.timezone.utc).timestamp()
                         if at else 0.0)
            except (TypeError, ValueError):
                stamp = 0.0
            out[key[7:]] = (int(demand.get("count") or 0), stamp)
        return out
    except Exception:  # a locked or absent DB must not stop a prune: then retention falls back to mtime
        return {}


def protected_listen_paths(copies: Iterable, fetching: Iterable[str], local_root: Path,
                           pinned: set[Path] | None = None) -> set[Path]:
    """Pinned paths and folders of server copies and local downloads in use."""
    paths = set(pinned or ())
    paths.update(cp.path.parent for cp in copies if cp.readers or not cp.settled())
    paths.update(local_root / hashlib.sha1(ref.encode()).hexdigest()[:16] for ref in fetching)
    return paths
