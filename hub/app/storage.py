"""Статистика диска для админки: сколько занято, чем занято и что в библиотеке.

Зачем отдельный модуль, а не запрос прямо в эндпоинте: сводка нужна админке, но её сборка — это рекурсивный обход
каталогов на сотни гигабайт (секунды на медленном диске) и пара агрегатов по БД. Если считать это на каждый открытый
экран, админка будет тормозить на ровном месте. Поэтому:
- обход каталогов и чтение дискового счётчика — синхронные (`os.scandir`, `shutil.disk_usage`), их вызов из async
  эндпоинта оборачивается в `asyncio.to_thread`, чтобы не блокировать цикл событий;
- готовый результат держится в памяти 60 секунд (`_cache`): цифры свободного места меняются медленно, а админка может
  открываться часто. Обход на несколько секунд, повторно бегать за ним в пределах минуты незачем.

Пути берутся из настроек ровно там, где их вычисляют рабочие модули: библиотека и её подкаталоги — свойства
`Settings` (`hub/app/config.py`), кеш yt-dlp — переменная окружения `YTDLP_CACHE_DIR` (как в `housekeeping.py`),
картинки ассистента — `data_dir / "assistant"` (как в `ai/pipeline.py`). Отсутствующий каталог — это 0, а не ошибка.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.models import Album, Track

# Сколько живёт закешированная сводка. Обход каталогов — секунды, поэтому админке не нужно пересчитывать его на
# каждое открытие; 60 секунд — компромисс между свежестью и нагрузкой на диск
CACHE_TTL_S = 60.0

# (время расчёта по monotonic, готовый словарь) — модульный кеш, как у прочих «горячих» ответов
_cache: tuple[float, dict[str, Any]] | None = None


def invalidate() -> None:
    """Сбросить сводку, чтобы после изменения config.yaml она пересчиталась сразу."""
    global _cache
    _cache = None


def _dir_size(root: Path) -> int:
    """Суммарный размер файлов под `root` (рекурсивно, без перехода по символическим ссылкам).

    Обход итеративный со своим стеком: у глубоких библиотек рекурсия упиралась бы в лимит. Любая ошибка доступа
    (каталог исчез, нет прав на файл) пропускается, а не роняет всю сводку — статистика не должна зависеть от
    одного битого файла. Отсутствующий каталог даёт 0.
    """
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        # follow_symlinks=False: ссылка на другой каталог не должна зациклить обход
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _disk_usage(path: Path) -> tuple[int, int, int]:
    """(всего, занято, свободно) в байтах для раздела, на котором лежит `path`. Нет пути — нули, а не исключение."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0, 0, 0
    return usage.total, usage.used, usage.free


def _dir_sizes() -> dict[str, int]:
    """Размеры всех каталогов, за которыми следит админка, одним синхронным проходом (вызывать в `to_thread`)."""
    s = get_settings()
    ytdlp = os.environ.get("YTDLP_CACHE_DIR")  # путь не в config.yaml: читается так же, как в housekeeping.py
    return {
        "library_bytes": _dir_size(s.library_dir),
        "trash_bytes": _dir_size(s.trash_dir),
        "uploads_bytes": _dir_size(s.staging_dir / "uploads"),
        # подкаталоги staging берём литералами: импорт `listen`/`light` тянул бы за собой весь стриминговый стек
        "listen_bytes": _dir_size(s.staging_dir / "listen"),  # listen.LOCAL_DIR
        "listen_light_bytes": _dir_size(s.staging_dir / "listen-light"),  # light.LIGHT_DIR
        "backups_bytes": _dir_size(s.backups_dir),
        "ytdlp_bytes": _dir_size(Path(ytdlp)) if ytdlp else 0,
        "assistant_bytes": _dir_size(s.data_dir / "assistant"),  # как в ai/pipeline.py: task_dir
    }


async def _db_stats_session(s: AsyncSession) -> dict[str, Any]:
    """Агрегаты по библиотеке из БД: треки, альбомы, сумма размеров и топ альбомов.

    Всё считается запросами с агрегатами (`count`/`sum`/`group by`), а не обходом строк в Питоне: треков бывают
    сотни тысяч, и тянуть их в память ради суммы нельзя.
    """
    # один запрос даёт и число треков, и сумму их размеров, и сколько размеров не заполнено (бэкфилл не прошёл)
    tracks, size_sum, without_size = (await s.execute(
        select(func.count(Track.id), func.sum(Track.size_bytes),
               func.sum(case((Track.size_bytes.is_(None), 1), else_=0)))
    )).one()
    albums = (await s.execute(select(func.count(Album.id)))).scalar_one()
    # «что занимает место»: 10 альбомов с наибольшим суммарным размером файлов
    rows = await s.execute(
        select(Album.id, Album.title, Album.album_artist, func.sum(Track.size_bytes), func.count(Track.id))
        .join(Track, Track.album_id == Album.id)
        .group_by(Album.id)
        .order_by(func.sum(Track.size_bytes).desc())
        .limit(10)
    )
    largest = [
        {"id": album_id, "title": title, "album_artist": artist or "",
         "size_bytes": int(total or 0), "tracks": int(count or 0)}
        for album_id, title, artist, total, count in rows
    ]
    return {
        "tracks": int(tracks or 0),
        "albums": int(albums or 0),
        "library_tracks_bytes": int(size_sum or 0),
        "tracks_without_size": int(without_size or 0),
        "largest_albums": largest,
    }


async def _db_stats(s: AsyncSession | None = None) -> dict[str, Any]:
    """Агрегаты из БД; эндпоинт передаёт свою сессию (`Depends(get_session)`), иначе открываем свою."""
    if s is not None:
        return await _db_stats_session(s)
    async with session_scope() as own:
        return await _db_stats_session(own)


def _build_overview() -> dict[str, Any]:
    """Synchronous disk/cache summary; music and staging may live on different filesystems."""
    s = get_settings()
    total, used, free = _disk_usage(s.music_root)
    cache_free = _disk_usage(s.staging_dir)[2]
    out: dict[str, Any] = {
        "disk_total": total, "disk_used": used, "disk_free": free, "cache_free": cache_free,
        "cache_limit": s.cfg.storage.listen_max_mb * 2**20,
        "autodownload_listened": s.cfg.storage.autodownload_listened,
    }
    out.update(_dir_sizes())
    return out


async def overview(s: AsyncSession | None = None) -> dict[str, Any]:
    """Сводка по диску и библиотеке для админки. Результат держится в памяти `CACHE_TTL_S` секунд.

    Повторный вызов раньше, чем через минуту, возвращает сохранённый словарь без обращения к диску и БД: обход
    каталогов на сотни гигабайт занимает секунды, а админка может открываться часто. Обход выполняется в
    `asyncio.to_thread` — это синхронный ввод-вывод, и он не должен блокировать цикл событий эндпоинта.

    `s` — сессия эндпоинта (или None: тогда модуль откроет свою). При попадании в кеш она не используется.
    """
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL_S:
        return _cache[1]
    result = await asyncio.to_thread(_build_overview)
    result.update(await _db_stats(s))
    _cache = (now, result)
    return result
