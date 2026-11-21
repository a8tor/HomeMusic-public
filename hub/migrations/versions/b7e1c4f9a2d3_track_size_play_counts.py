"""Track file sizes, play counters, and immutable author/provenance snapshots."""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath

from alembic import op
import sqlalchemy as sa

revision = "b7e1c4f9a2d3"
down_revision = "3f7b1a8e2c60"
branch_labels = depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.add_column("tracks", sa.Column("added_by_username", sa.String(128)))
    op.add_column("tracks", sa.Column("origin", sa.String(16)))
    op.add_column("tracks", sa.Column("size_bytes", sa.Integer()))
    op.add_column("tracks", sa.Column("play_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tracks", sa.Column("last_played_at", sa.DateTime()))

    bind = op.get_bind()
    _backfill_size_bytes(bind)
    _backfill_play_counters(bind)
    _backfill_author_and_origin(bind)


def downgrade() -> None:
    for name in ("last_played_at", "play_count", "size_bytes", "origin", "added_by_username"):
        op.drop_column("tracks", name)




def _music_root() -> Path:
    # Keep migration path resolution independent from the current application/ORM schema.
    return Path(os.environ.get("MUSIC_ROOT", "/music"))


def _backfill_size_bytes(bind) -> None:
    tracks = sa.Table("tracks", sa.MetaData(), autoload_with=bind)
    library = _music_root() / "library"
    updated = missing = 0
    for track_id, rel in bind.execute(sa.select(tracks.c.id, tracks.c.path)).all():
        if not rel:
            continue
        path = library.joinpath(*PurePosixPath(rel).parts)
        try:
            size = os.path.getsize(path)
        except OSError:
            missing += 1
            continue
        bind.execute(sa.update(tracks).where(tracks.c.id == track_id).values(size_bytes=size))
        updated += 1
    log.info("tracks.size_bytes backfill: %s set, %s files missing", updated, missing)


def _backfill_play_counters(bind) -> None:
    tracks = sa.Table("tracks", sa.MetaData(), autoload_with=bind)
    events = sa.Table("play_events", tracks.metadata, autoload_with=bind)
    # Start events are the app's measured listens. Navidrome finish rows are threshold scrobbles.
    real_play = sa.and_(
        events.c.kind == "start",
        events.c.client != "navidrome",
    )
    scrobble = sa.and_(
        events.c.kind == "finish",
        events.c.client == "navidrome",
        events.c.superseded_scrobble.is_(False),
    )
    matched = sa.and_(events.c.nd_song_id == tracks.c.nd_song_id, sa.or_(real_play, scrobble))
    count = sa.select(sa.func.count(events.c.id)).where(matched).correlate(tracks).scalar_subquery()
    latest = sa.select(sa.func.max(events.c.ts)).where(matched).correlate(tracks).scalar_subquery()
    has_play = sa.exists().where(matched).correlate(tracks)
    bind.execute(sa.update(tracks).where(tracks.c.nd_song_id.is_not(None), has_play)
                 .values(play_count=count, last_played_at=latest))


def _backfill_author_and_origin(bind) -> None:
    metadata = sa.MetaData()
    tracks = sa.Table("tracks", metadata, autoload_with=bind)
    users = sa.Table("users", metadata, autoload_with=bind)
    downloads = sa.Table("download_items", metadata, autoload_with=bind)
    uploads = sa.Table("uploads", metadata, autoload_with=bind)
    imports = sa.Table("import_items", metadata, autoload_with=bind)

    # Snapshot names only where the old foreign key actually identifies the author.
    bind.execute(sa.update(tracks).where(tracks.c.added_by.is_not(None)).values(
        added_by_username=sa.select(users.c.nd_username)
        .where(users.c.id == tracks.c.added_by).scalar_subquery()))

    download_exists = sa.exists(sa.select(downloads.c.id).where(downloads.c.track_id == tracks.c.id))
    upload_exists = sa.exists(sa.select(uploads.c.id).where(uploads.c.track_id == tracks.c.id))
    import_exists = sa.exists(sa.select(imports.c.id).where(imports.c.track_id == tracks.c.id))
    # Existing rows are classified only from their real source relations. Unknown legacy files stay NULL.
    bind.execute(sa.update(tracks).where(upload_exists).values(origin="upload"))
    bind.execute(sa.update(tracks).where(import_exists, tracks.c.origin.is_(None)).values(origin="import"))
    bind.execute(sa.update(tracks).where(download_exists, tracks.c.origin.is_(None)).values(origin="download"))
