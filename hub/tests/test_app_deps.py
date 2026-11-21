"""The DB session must be committed before the response leaves (no stale reads right after a write)."""

from pathlib import Path

from fastapi.routing import APIRoute

from app.db import get_session
from app.main import app


def _walk(dep):
    yield dep
    for d in dep.dependencies:
        yield from _walk(d)


def _routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif (sub := getattr(r, "original_router", None)) is not None:  # FastAPI >= 0.14x wraps included routers
            yield from _routes(sub.routes)


def test_db_session_is_function_scoped() -> None:
    seen = 0
    for route in _routes(app.routes):
        for d in _walk(route.dependant):
            if d.call is get_session:
                seen += 1
                assert d.scope == "function", f"{route.path}: Depends(get_session) without scope='function'"
    assert seen > 10


def test_editable_config_reload(monkeypatch, tmp_path: Path) -> None:
    from app import config

    path = tmp_path / "config.yaml"
    path.write_text("sources:\n  default: [hitmoz]\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(path))
    config.get_settings.cache_clear()
    config.write_editable_config(path, {
        "trash_days": 12,
        "listen_keep_s": 7200,
        "listen_max_mb": 321,
        "listen_min_free_mb": 256,
        "ytdlp_cache_days": 9,
        "assistant_days": 11,
        "autodownload_listened": True,
    })

    reloaded = config.reload_settings()
    assert reloaded.cfg.library.trash_days == 12
    assert reloaded.cfg.storage.listen_max_mb == 321
    assert reloaded.cfg.sources.default == ["hitmoz"]
