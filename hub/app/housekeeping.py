"""Periodic maintenance: DB backup, trash/staging cleanup, cache purge, provider smoke test (NFR-5, NFR-6)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.config import get_settings
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

KEEP_BACKUPS = 7


def backup_hub_db() -> Path | None:
    s = get_settings()
    if not s.db_path.is_file():
        return None
    dest_dir = s.backups_dir / "hub"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"hub-{datetime.now():%Y%m%d}.sqlite3"
    tmp = dest_dir / ".hub-backup.part"
    # backup into the WSL2 filesystem first, then copy: SQLite file locking on the Windows mount is unreliable
    local_tmp = s.data_dir / "backup.tmp.sqlite3"
    src = sqlite3.connect(s.db_path)
    try:
        dst = sqlite3.connect(local_tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.write_bytes(local_tmp.read_bytes())
    tmp.replace(dest)
    local_tmp.unlink(missing_ok=True)
    for old in sorted(dest_dir.glob("hub-*.sqlite3"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return dest


def prune_old_files(folder: Path, days: int, *, now: float | None = None) -> int:
    """Deletes files under `folder` not modified for `days` days and then the folders left empty. Returns the number of
    files removed. A missing folder is not an error: the cache may never have been used."""
    if not folder.is_dir():
        return 0
    limit = (time.time() if now is None else now) - days * 86400
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(folder, topdown=False):
        for name in filenames:
            f = Path(dirpath) / name
            try:
                if f.stat().st_mtime >= limit:
                    continue
                f.unlink()
                removed += 1
            except OSError as e:
                log.warning("housekeeping: %s not removed: %s", f, e)
        # bottom-up: a folder is empty only once its own subfolders are gone
        if Path(dirpath) != folder:
            try:
                Path(dirpath).rmdir()
            except OSError:
                pass  # not empty
    return removed


def prune_assistant(days: int, *, now: float | None = None, protected: set[str] | None = None) -> int:
    """Remove old task images only when no live task or saved assistant card references them."""
    root = get_settings().data_dir / "assistant"
    if not root.is_dir():
        return 0
    limit = (time.time() if now is None else now) - days * 86400
    removed = 0
    protected = protected or set()
    for d in list(root.iterdir()):
        if not d.is_dir() or d.is_symlink() or d.name in protected:
            continue
        try:
            files = [f for f in d.rglob("*") if f.is_file() and not f.is_symlink()]
            newest = max((f.stat().st_mtime for f in files), default=0.0)
            if not files or newest >= limit:
                continue
            shutil.rmtree(d)
            removed += 1
        except OSError as e:
            log.warning("housekeeping: assistant dir %s not removed: %s", d, e)
    return removed


async def smoke_test_providers() -> dict[str, str]:
    """Search a well-known track on every provider; a parser that returns nothing means the layout changed."""
    from app.providers.base import TrackQuery
    from app.services import svc

    q = TrackQuery(artist="Кино", title="Группа крови")
    out: dict[str, str] = {}
    for name, p in svc.providers.items():
        try:
            res = await p.search(q, limit=5)
            out[name] = f"ok ({len(res)})" if res else "EMPTY — возможно, сменилась вёрстка"
        except Exception as e:
            out[name] = f"error: {e!r}"[:200]
    bad = {k: v for k, v in out.items() if not v.startswith("ok")}
    (log.warning if bad else log.info)("provider smoke test: %s", out)
    return out


async def run_daily() -> None:
    from app.accounts import purge_email_codes
    from app.api.uploads import purge_stale_uploads
    from app.catalog.cache import cache_purge_expired
    from app.library.editor import purge_trash

    cfg = get_settings().cfg
    try:
        path = await asyncio.to_thread(backup_hub_db)
        log.info("hub db backup: %s", path)
    except Exception:
        log.exception("hub db backup failed")
    try:
        n = await asyncio.to_thread(purge_trash, cfg.library.trash_days)
        m = await asyncio.to_thread(purge_stale_uploads)
        c = await cache_purge_expired()
        e = await purge_email_codes()
        log.info("housekeeping: trash dirs %s, stale uploads %s, cache rows %s, email codes %s removed", n, m, c, e)
    except Exception:
        log.exception("housekeeping failed")
    # The staging caches are pruned when a copy is made (and after an encode); while nobody listens or encodes they
    # only grow. Each step is its own try: a stuck cache must not skip the rest.
    try:
        from app import listen

        await listen.prune_cache()
        log.info("housekeeping: shared staging cache pruned (soft limit %s MB)", cfg.storage.listen_max_mb)
    except Exception:
        log.exception("staging cache prune failed")
    try:
        # yt-dlp keeps its scripts, tokens and fragment indexes here (YTDLP_CACHE_DIR, set by Compose); the path is
        # not in config.yaml, so it is read the way the provider reads it
        cache_dir = os.environ.get("YTDLP_CACHE_DIR")
        if cache_dir:
            n = await asyncio.to_thread(prune_old_files, Path(cache_dir), cfg.storage.ytdlp_cache_days)
            log.info("housekeeping: yt-dlp cache %s: %s files removed", cache_dir, n)
    except Exception:
        log.exception("yt-dlp cache prune failed")
    try:
        from sqlalchemy import select
        from app.models import AssistantTask, AssistantChat
        from app.db import session_scope
        async with session_scope() as s:
            active = set((await s.execute(select(AssistantTask.id).where(
                AssistantTask.status.in_(("queued", "extracting", "resolving", "awaiting_confirmation"))))).scalars())
            chat_rows = (await s.execute(select(AssistantChat.messages))).scalars()
            image_refs: set[str] = set()
            for messages in chat_rows:
                for message in messages or []:
                    images = message.get("images", []) if isinstance(message, dict) else []
                    for image in images if isinstance(images, list) else []:
                        # a raw string: `\d`, not `\\d`, or the reference to an existing image is never found
                        match = re.search(r"/assistant/(\d+)(?:/|$)", str(image))
                        if match:
                            image_refs.add(match.group(1))
        protected = {str(i) for i in active} | image_refs
        n = await asyncio.to_thread(prune_assistant, cfg.storage.assistant_days, protected=protected)
        log.info("housekeeping: assistant image dirs %s removed", n)
    except Exception:
        log.exception("assistant image prune failed")
    await smoke_test_providers()


async def scheduler_loop() -> None:
    """Runs `run_daily` at cfg.library.backup_hour local time every day."""
    hour = get_settings().cfg.library.backup_hour
    while True:
        now = datetime.now()
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await run_daily()
