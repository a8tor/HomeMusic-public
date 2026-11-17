"""Provider search returns fast results even when another source times out."""

import asyncio
from types import SimpleNamespace

import pytest

from app.api import search
from app.api.schemas import ProviderSearchOut
from app.providers.base import Candidate


class Provider:
    def __init__(self, result=(), delay=0):
        self.result = result
        self.delay = delay

    async def search(self, query, limit=10):
        await asyncio.sleep(self.delay)
        return list(self.result)


@pytest.mark.asyncio
async def test_provider_timeout_keeps_fast_results(monkeypatch):
    fast_candidate = Candidate(provider="fast", provider_id="1", artist="Кино", title="Группа крови",
                               page_url="https://fast.test/1")
    monkeypatch.setattr(search, "get_settings", lambda: SimpleNamespace(provider_search_timeout_s=0.01))
    monkeypatch.setattr(search.svc, "providers", {
        "fast": Provider([fast_candidate]),
        "slow": Provider(delay=0.05),
    })
    monkeypatch.setattr(search.svc, "reupload_providers", lambda: [])
    monkeypatch.setattr(search.listen, "remember_files", _noop)

    result = await search.search_providers(
        q="Кино", provider=None, group=None,
        user=SimpleNamespace(id=1),
    )

    assert isinstance(result, ProviderSearchOut)
    assert [candidate.provider for candidate in result.candidates] == ["fast"]
    assert result.errors == ["slow: таймаут поиска"]


async def _noop(candidates):
    return None
