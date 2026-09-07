"""Tests for the smart içtihat search core (_run_ictihat_arama) compact output."""
from types import SimpleNamespace

import mcp_server_main as m


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    def __init__(self, entries):
        self._entries = entries

    async def search_documents_multi(self, requests):
        entries = self._entries
        data = SimpleNamespace(emsalKararList=entries, total=len(entries))
        return [_FakeResponse(data) for _ in requests]


def _entry(doc_id, **overrides):
    fields = {
        "documentId": doc_id,
        "itemType": "YARGITAYKARARI",
        "birimId": None,
        "birimAdi": "1. Hukuk Dairesi",
        "esasNoYil": 2023,
        "esasNoSira": 1234,
        "kararNoYil": 2024,
        "kararNoSira": 567,
        "kararTuru": None,
        "kararTarihi": "2024-05-05T00:00:00.000Z",
        "kararTarihiStr": "05.05.2024",
        "kesinlesmeDurumu": None,
        "kararNo": "2024/567",
        "esasNo": "2023/1234",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_run_ictihat_arama_returns_compact_decisions(monkeypatch):
    entries = [_entry("d-1"), _entry("d-2")]
    monkeypatch.setattr(m, "bedesten_client_instance", _FakeClient(entries))
    import asyncio

    result = asyncio.run(m._run_ictihat_arama("tapu iptali tescil"))
    decisions = result["decisions"]
    assert len(decisions) == 2
    assert decisions[0]["documentId"] == "d-1"
    assert decisions[0]["court_type"] == "YARGITAYKARARI"
    assert decisions[0]["esasNo"] == "2023/1234"
    assert decisions[0]["kararNo"] == "2024/567"
    assert decisions[0]["kararTarihiStr"] == "05.05.2024"
    assert decisions[0]["source_url"] == "https://mevzuat.adalet.gov.tr/ictihat/d-1"
    for field in ("esasNoYil", "esasNoSira", "kararNoYil", "kararNoSira", "kesinlesmeDurumu", "kararTuru", "birimId", "itemType"):
        assert field not in decisions[0], f"redundant field {field} leaked into output"


def test_run_ictihat_arama_deduplicates(monkeypatch):
    import asyncio

    entries = [_entry("d-1"), _entry("d-1")]
    monkeypatch.setattr(m, "bedesten_client_instance", _FakeClient(entries))
    result = asyncio.run(m._run_ictihat_arama("miras muvazaa"))
    assert len(result["decisions"]) == 1
