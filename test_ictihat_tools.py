"""Standalone live test for the smart içtihat tools (ictihat_ara / ictihat_getir / karar_atif_zinciri).

Run with:  uv run python test_ictihat_tools.py

Makes ~5 Bedesten requests total (1 search + 1 document + 3 citation resolutions),
which fits within the upstream rate limit (~10 req / 30s). Skips gracefully if
the upstream API is unreachable (exit code 2).
"""
import asyncio
import json
import sys

from fastmcp import Client

from mcp_server_main import app

QUERY = "Yargıtay 1. Hukuk Dairesi muris muvazaası tapu iptali ve tescil"


def _text(result) -> str:
    content = getattr(result, "content", None)
    if content:
        return content[0].text
    if isinstance(result, list) and result and hasattr(result[0], "text"):
        return result[0].text
    return json.dumps(result, ensure_ascii=False, default=str)


async def run() -> int:
    async with Client(app) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        required = {"ictihat_ara", "ictihat_getir", "karar_atif_zinciri"}
        missing = required - names
        if missing:
            print(f"FAIL: missing tools {missing}")
            return 1

        print("== 1/3 ictihat_ara ==")
        res = await client.call_tool("ictihat_ara", {"sorgu": QUERY})
        data = json.loads(_text(res))
        decisions = data.get("decisions") or []
        print(f"decisions={len(decisions)} total={data.get('total_records')} analiz={data.get('analiz', {}).get('court_types')}")
        if data.get("error"):
            print(f"SKIP: upstream error: {data['error']}")
            return 2
        if not decisions:
            print("SKIP: no decisions returned")
            return 2
        first = decisions[0]
        for field in ("documentId", "court_type", "esasNo", "kararNo", "kararTarihiStr", "source_url"):
            assert field in first, f"compact output missing {field}"
        assert "ozet" in data and data["ozet"], "readable summary (ozet) missing"
        print(f"first: {first['documentId']} {first['birimAdi']} {first['esasNo']} {first['kararNo']}")
        assert "esasNoYil" not in first, "compact output leaked redundant field"

        print("== 2/3 ictihat_getir ==")
        doc = first["documentId"]
        res = await client.call_tool("ictihat_getir", {"documentId": doc, "ozet_uzunlugu": 400})
        data = json.loads(_text(res))
        content = data.get("markdown_content") or ""
        print(f"documentId={data.get('documentId')} mime={data.get('mime_type')} chars={len(content)} ozet={len(data.get('ozet') or '')}")
        if data.get("error_message"):
            print(f"SKIP: {data['error_message']}")
            return 2
        assert len(content) > 200, "markdown content too short"

        print("== 3/3 karar_atif_zinciri (maks_atif=2, eslesme_limiti=2) ==")
        res = await client.call_tool("karar_atif_zinciri", {"documentId": doc, "maks_atif": 2, "eslesme_limiti": 2})
        data = json.loads(_text(res))
        if data.get("error_message"):
            print(f"SKIP: {data['error_message']}")
            return 2
        print(f"toplam_ayrinti={data.get('toplam_ayrinti')} cozulen={data.get('cozulen')} atif_alanlar={len(data.get('atif_alanlar') or [])}")
        for c in (data.get("atiflar") or [])[:2]:
            print(f"  atif {c.get('karar_no') or c.get('esas_no')} -> {len(c.get('eslesme') or [])} eslesme")

        print("ALL İÇTİHAT TOOL TESTS PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
