import sqlite3
from contextlib import closing

import numpy as np
import pytest

from app.backfill_energy import backfill_energy
from app.db import SCHEMA, Store
from app.energy import CALM_PROMPTS, ENERGETIC_PROMPTS, build_energy_anchor, energy_score, project_energy
from app.models import FakeModel
from app.worker import Prepared

from .support import running


def test_pcm_energy_on_synthetic_audio():
    sr = 24000
    t = np.arange(8 * sr) / sr
    rng = np.random.default_rng(17)
    sine = np.sin(2 * np.pi * 440 * t)
    phase = t % (1 / 3)  # 180 BPM kick with two offbeat hats per beat.
    kick = np.sin(2 * np.pi * 100 * phase) * np.exp(-phase / 0.045)
    hat_phase = phase % (1 / 9)
    hat = np.cos(2 * np.pi * 5500 * t) * np.exp(-hat_phase / 0.016)
    rhythm = np.clip(kick + hat, -1, 1)
    clipped = np.clip(30 * sine, -1, 1)

    quiet, steady, busy, dense, noise = [
        energy_score(w, sr) for w in [0.02 * sine, 0.8 * sine, rhythm, clipped, rng.normal(0, 0.1, t.size)]
    ]
    assert 0 < quiet < steady < 0.35
    assert steady < busy and busy > 0.5
    assert steady < dense and dense > 0.6
    assert 0 <= noise <= 1
    assert energy_score(np.zeros_like(t), sr) == energy_score(np.array([]), sr) == 0.0
    assert energy_score(np.array([np.nan, np.inf]), sr) == energy_score(sine, 0) == 0.0
    assert 0 < energy_score(np.array([0.9]), sr) <= 1


def test_zero_shot_anchor_orders_prompt_centroids():
    model = FakeModel()
    anchor = build_energy_anchor(model.embed_text)
    high = project_energy(model.embed_text(list(ENERGETIC_PROMPTS)).mean(axis=0), anchor)
    low = project_energy(model.embed_text(list(CALM_PROMPTS)).mean(axis=0), anchor)
    assert high is not None and low is not None
    assert 0 <= low.score < high.score <= 1
    assert -1 <= low.raw < high.raw <= 1
    assert project_energy(np.zeros(model.dim), anchor) is None
    assert not model.audio_calls


async def test_energy_endpoint_and_indexing(settings, monkeypatch):
    async with running(settings) as c:
        st = c.app.state
        empty = await c.post("/energy", json={"keys": ["missing"]})
        assert empty.json() == {"missing": None}
        assert st.engine.model is None
        assert (await c.post("/energy", json={"keys": ["x"] * 200})).status_code == 200
        assert (await c.post("/energy", json={"keys": ["x"] * 201})).status_code == 422

        text_calls = []
        embed_text = c.model.embed_text

        def count_text(texts):
            text_calls.append(texts)
            return embed_text(texts)

        monkeypatch.setattr(c.model, "embed_text", count_text)
        model = st.engine.name
        # Index two batches without ffmpeg; verify the anchor is built only once.
        for key, freq in [("first", 220), ("second", 440)]:
            st.store.enqueue([(key, "path", key)], force=False, model=model)
            wav = np.sin(2 * np.pi * freq * np.arange(c.model.sr) / c.model.sr)
            await st.worker.embed(Prepared(st.store.claim(1), [wav], 0.0))
        assert text_calls == [[*ENERGETIC_PROMPTS, *CALM_PROMPTS]]
        st.store.enqueue([("queued", "path", "queued")], force=False, model=model)
        response = await c.post("/energy", json={"keys": ["first", "missing", "queued"]})
        assert response.status_code == 200
        score = response.json()["first"]
        assert isinstance(score, float) and 0 <= score <= 1
        assert response.json() == {"first": score, "missing": None, "queued": None}
        assert len(text_calls) == 1
        assert (await c.get("/embed/first")).json()["energy"] == score
        similar = await c.post("/similarity", json={"candidates": ["first"], "profile": [{"key": "second", "weight": 1}]})
        assert similar.json()["results"][0]["energy"] == score
        found = await c.post("/text", json={"text": "fast music", "keys": ["first"]})
        assert found.json()["results"][0]["energy"] == score


def test_backfill_migrates_and_only_fills_missing_energy(settings):
    path = settings.db_path
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA.replace("    energy REAL,\n", ""))
        model = FakeModel()
        anchor = build_energy_anchor(model.embed_text).astype(np.float32)
        conn.executemany(
            "INSERT INTO items(key, kind, source, status, model, dim, vector, created_at, updated_at) "
            "VALUES (?, 'path', 'missing.wav', 'done', 'fake', ?, ?, 0, 0)",
            [(key, model.dim, vec.tobytes()) for key, vec in [("a", anchor), ("b", -anchor)]],
        )

    with closing(Store(path)) as store:
        assert store.energies(["a", "b"], "fake") == {"a": None, "b": None}
        store.enqueue([("kept", "path", "missing.wav")], force=False, model="fake")
        store.claim(1)
        store.finish("kept", anchor, "fake", 1.0, energy=0.24)
        assert backfill_energy(1, settings=settings, factory=lambda _: model) == 1
        assert store.energies(["a", "b", "kept"], "fake") == {"a": pytest.approx(1), "b": None, "kept": 0.24}
        assert backfill_energy(10, settings=settings, factory=lambda _: model) == 1
        assert backfill_energy(10, settings=settings, factory=lambda _: pytest.fail("unnecessary model load")) == 0
        assert store.get("b").energy == pytest.approx(0)
        assert store.get("kept").energy == 0.24
        assert not model.audio_calls
