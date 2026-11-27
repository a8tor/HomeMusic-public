"""Manually backfill energy from existing vectors: python -m app.backfill_energy --limit 10000."""

import argparse
from contextlib import closing

from .config import Settings
from .db import Store, from_blob
from .energy import build_energy_anchor, project_energy
from .models import FakeModel, MuQMuLan, load_model


def backfill_energy(limit: int, *, settings: Settings | None = None, factory=load_model) -> int:
    """Inspect at most `limit` eligible rows and return the number actually updated."""
    if limit < 1:
        raise ValueError("limit must be positive")
    settings = settings or Settings()
    name = "fake" if settings.model == "fake" else settings.model_repo
    if not settings.db_path.is_file():
        raise FileNotFoundError(settings.db_path)
    updated = 0
    after = anchor = None
    with closing(Store(settings.db_path)) as store:
        # Avoid loading the model when there's no work. The configured spaces have fixed dimensions.
        dim = FakeModel.dim if settings.model == "fake" else MuQMuLan.dim
        while limit:
            rows = store.missing_energy_vectors(name, dim, min(limit, 500), after)
            if not rows:
                break
            if anchor is None:
                with closing(factory(settings)) as model:
                    anchor = build_energy_anchor(model.embed_text)
                if len(anchor) != dim:
                    raise ValueError("energy anchor dimension does not match the configured vector space")
            updates = []
            for key, blob in rows:
                projection = project_energy(from_blob(blob), anchor)
                if projection is not None:
                    updates.append((projection.score, key, blob))
            if updates:
                updated += store.fill_energy(name, updates)
            limit -= len(rows)
            after = rows[-1][0]
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10000, help="maximum eligible rows to inspect (default: 10000)")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    print(f"Updated energy for {backfill_energy(args.limit)} tracks")


if __name__ == "__main__":
    main()
