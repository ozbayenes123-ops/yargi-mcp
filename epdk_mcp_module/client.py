# epdk_mcp_module/client.py

import asyncio
import io
import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .models import (
    EpdkSearchRequest,
    EpdkKararSummary,
    EpdkSearchResult,
    EpdkDocumentMarkdown
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
EPDK_CACHE_TTL_SECONDS = 12 * 3600
MAX_CONCURRENT_FAST_ACCESS = 8

PIYASA_PAGES = {
    "elektrik": "/Detay/Icerik/3-0-39/kurul-kararlari-",
    "dogalgaz": "/Detay/Icerik/3-0-19/kurul-kararlari",
    "petrol": "/Detay/Icerik/3-0-100/kurul-kararlari",
    "lpg": "/Detay/Icerik/3-0-101/kurul-kararlari",
    "denetim": "/Detay/Icerik/3-0-136/kurul-kararlari",
    "enerji_donusumu": "/Detay/Icerik/3-0-206/kurul-kararlari",
}

FAST_ACCESS_ENDPOINT = "https://epdk.gov.tr/Detay/GetFastAccessList"
DOWNLOAD_ENDPOINT = "https://epdk.gov.tr/Detay/DownloadDocument"

PREFERRED_CONTENT_TYPES = [11, 8, 9, 7]  # word, pdf, excel, zip


def _fold(text: str) -> str:
    """Turkish-diacritic-insensitive fold for local keyword filtering."""
    if not text:
        return ""
    t = text.lower()
    t = t.replace("\u0131", "i")       # ı -> i
    t = t.replace("i\u0307", "i")      # İ lower -> i + combining dot
    t = t.replace("\u015f", "s").replace("\u00e7", "c")
    t = t.replace("\u00f6", "o").replace("\u00fc", "u")
    t = t.replace("\u011f", "g").replace("\u00e2", "a")
    t = t.replace("\u00ee", "i").replace("\u00fb", "u")
    return t


class EpdkApiClient:
    """
    API client for searching EPDK (Enerji Piyasası Düzenleme Kurumu) Kurul
    kararları directly from epdk.gov.tr:
      - Crawls each piyasa (sektör) page for FastAccess category ids (data-id)
      - POST /Detay/GetFastAccessList per category id to collect karar items
      - Caches the collected index per piyasa to disk (JSON + TTL)
      - Filters items locally by keywords (title/number, diacritic-insensitive)
      - Downloads belge via /Detay/DownloadDocument?id=<ContentId>
    """

    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000

    def __init__(self, request_timeout: float = 60.0):
        self.http_client = httpx.AsyncClient(
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "X-Requested-With": "XMLHttpRequest"
            },
            timeout=request_timeout,
            follow_redirects=True
        )
        self._crawl_cache: Dict[str, List[Dict[str, Any]]] = {}

    @staticmethod
    def _cache_path(piyasa: str) -> Path:
        return CACHE_DIR / f"epdk_{piyasa}.json"

    def _read_cache(self, piyasa: str) -> Optional[List[Dict[str, Any]]]:
        try:
            path = self._cache_path(piyasa)
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = data.get("fetched_at", 0)
            if (datetime.now().timestamp() - fetched_at) > EPDK_CACHE_TTL_SECONDS:
                return None
            return data.get("items") or []
        except Exception as e:
            logger.warning(f"EpdkApiClient: cache read failed for {piyasa}: {e}")
            return None

    def _write_cache(self, piyasa: str, items: List[Dict[str, Any]]):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = self._cache_path(piyasa)
            path.write_text(
                json.dumps({"fetched_at": datetime.now().timestamp(), "items": items},
                           ensure_ascii=False, indent=1),
                encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"EpdkApiClient: cache write failed for {piyasa}: {e}")

    @staticmethod
    def _category_path(a_tag: Any) -> List[str]:
        """Ancestor <li> labels for a category link (e.g. Elektrik Piyasası > TORETOSAF)."""
        path: List[str] = []
        node = a_tag
        while True:
            li = node.find_parent("li")
            if li is None:
                break
            label_a = li.find("a", recursive=False)
            if label_a:
                label = label_a.get_text(" ", strip=True)
                if label:
                    path.append(label)
            node = li
        path.reverse()
        return path

    async def _fetch_fast_access(self, f_id: int) -> Optional[Dict[str, Any]]:
        try:
            response = await self.http_client.post(
                FAST_ACCESS_ENDPOINT,
                json={"fId": f_id}
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("State") != 1:
                return None
            model = payload.get("model") or []
            return {
                "type": payload.get("Type"),
                "items": model
            }
        except Exception as e:
            logger.warning(f"EpdkApiClient: GetFastAccessList failed for fId={f_id}: {e}")
            return None

    async def _crawl_piyasa(self, piyasa: str) -> List[Dict[str, Any]]:
        """Fetch category ids from the piyasa page, then collect all karar items."""
        page_url = f"https://epdk.gov.tr{PIYASA_PAGES[piyasa]}"
        response = await self.http_client.get(page_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        category_ids: List[int] = []
        for a_tag in soup.select("a[data-id]"):
            raw = a_tag.get("data-id")
            if raw and raw.isdigit():
                category_ids.append(int(raw))
        category_ids = list(dict.fromkeys(category_ids))

        logger.info(f"EpdkApiClient: piyasa={piyasa} has {len(category_ids)} category ids")

        items: List[Dict[str, Any]] = []
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FAST_ACCESS)

        async def safe_fetch(f_id: int, path: List[str]):
            async with semaphore:
                result = await self._fetch_fast_access(f_id)
            if not result:
                return
            for item in result.get("items") or []:
                items.append({
                    "piyasa": piyasa,
                    "kategori": " > ".join(p for p in path if p),
                    "item": item
                })

        tasks = []
        for f_id in category_ids:
            a_tag = soup.find("a", attrs={"data-id": str(f_id)})
            path = self._category_path(a_tag) if a_tag else []
            tasks.append(safe_fetch(f_id, path))
        await asyncio.gather(*tasks, return_exceptions=True)

        # note: kategori paths collected eagerly above; re-map via dict
        logger.info(f"EpdkApiClient: piyasa={piyasa} collected {len(items)} items")
        return items

    async def _get_index(self, piyasa: str) -> List[Dict[str, Any]]:
        """Cached piyasa index (raw items from GetFastAccessList)."""
        cached = self._read_cache(piyasa)
        if cached is not None:
            return cached
        items = await self._crawl_piyasa(piyasa)
        self._write_cache(piyasa, items)
        return items

    @staticmethod
    def _pick_content_id(item: Dict[str, Any]) -> Optional[str]:
        details = item.get("FastAccessDetail") or []
        if not details:
            return None
        for pref in PREFERRED_CONTENT_TYPES:
            for d in details:
                if d.get("ContentType") == pref:
                    return str(d.get("ContentId"))
        return str(details[0].get("ContentId"))

    def _matches(self, item: Dict[str, Any], tokens: List[str]) -> bool:
        if not tokens:
            return True
        haystack = " ".join([
            str(item.get("Title") or ""),
            str(item.get("Number") or ""),
            str(item.get("HtmlDescription") or "")
        ])
        haystack = _fold(haystack)
        return all(t in haystack for t in tokens)

    async def search_decisions(self, params: EpdkSearchRequest) -> EpdkSearchResult:
        """Search EPDK Kurul kararları within a piyasa (local keyword filter over cached index)."""
        piyasa = params.piyasa or "elektrik"
        if piyasa not in PIYASA_PAGES:
            return EpdkSearchResult(
                decisions=[], total_results=0, page=params.page,
                pageSize=params.pageSize, query=params.keywords, piyasa=piyasa
            )

        try:
            index = await self._get_index(piyasa)
        except Exception as e:
            logger.error(f"EpdkApiClient: index crawl failed for {piyasa}: {e}")
            return EpdkSearchResult(
                decisions=[], total_results=0, page=params.page,
                pageSize=params.pageSize, query=params.keywords, piyasa=piyasa
            )

        tokens = [_fold(t) for t in (params.keywords or "").split() if t.strip()]
        matched: List[Dict[str, Any]] = []
        for entry in index:
            item = entry.get("item") or {}
            if item.get("Type") not in (None, 1):
                continue
            if not self._matches(item, tokens):
                continue
            content_id = self._pick_content_id(item)
            matched.append(EpdkKararSummary(
                baslik=item.get("Title"),
                karar_no=item.get("Number"),
                karar_tarihi=item.get("Date"),
                rg_tarihi=item.get("RgDate"),
                rg_sayisi=item.get("RgNumber"),
                piyasa=piyasa,
                kategori=entry.get("kategori"),
                mulga=item.get("MulgaTitle"),
                dosya_sayisi=len(item.get("FastAccessDetail") or []),
                content_id=content_id,
                document_id=f"epdk:{content_id}" if content_id else None
            ))

        matched.sort(key=lambda s: (s.karar_tarihi or ""), reverse=True)
        total = len(matched)
        start = (params.page - 1) * params.pageSize
        page_items = matched[start:start + params.pageSize]

        cache_yasi = None
        cached = self._read_cache(piyasa)
        if cached is not None:
            try:
                path = self._cache_path(piyasa)
                fetched_at = json.loads(path.read_text(encoding="utf-8")).get("fetched_at", 0)
                mins = int((datetime.now().timestamp() - fetched_at) / 60)
                cache_yasi = f"{mins} dakika"
            except Exception:
                pass

        logger.info(f"EpdkApiClient: piyasa={piyasa} matched {total} karar for {params.keywords!r}")
        return EpdkSearchResult(
            decisions=page_items,
            total_results=total,
            page=params.page,
            pageSize=params.pageSize,
            query=params.keywords,
            piyasa=piyasa,
            cache_yasi=cache_yasi
        )

    async def get_document(
        self,
        content_id: str,
        baslik: Optional[str] = None,
        karar_no: Optional[str] = None,
        karar_tarihi: Optional[str] = None,
        page_number: int = 1
    ) -> EpdkDocumentMarkdown:
        """Download an EPDK belge (docx/pdf/excel) and return paginated Markdown."""
        source_id = f"epdk:{content_id}"
        source_url = f"{DOWNLOAD_ENDPOINT}?id={content_id}"
        logger.info(f"EpdkApiClient: downloading document {content_id}")

        try:
            response = await self.http_client.get(source_url)
            response.raise_for_status()
            raw = response.content
            if not raw:
                raise ValueError("Boş belge indirildi.")

            belge_turu = "belge"
            text = ""
            if raw.startswith(b"%PDF"):
                belge_turu = "pdf"
                reader = PdfReader(io.BytesIO(raw))
                pages = []
                for pg in reader.pages:
                    pages.append(pg.extract_text() or "")
                text = "\n\n".join(pages)
            elif raw[:2] == b"PK":
                belge_turu = "docx"
                from markitdown import MarkItDown
                md_converter = MarkItDown(enable_plugins=False)
                md_result = md_converter.convert_stream(io.BytesIO(raw))
                text = md_result.text_content or ""
            elif raw.startswith(b"\xd0\xcf\x11\xe0"):
                belge_turu = "xls"
                text = ""
            else:
                belge_turu = "belge"
                text = ""

            if not text.strip():
                return EpdkDocumentMarkdown(
                    source_id=source_id, source_url=source_url,
                    baslik=baslik, karar_no=karar_no, karar_tarihi=karar_tarihi,
                    belge_turu=belge_turu,
                    markdown_chunk=None, current_page=page_number,
                    total_pages=0, is_paginated=False,
                    error_message="Belge metni çıkarılamadı (belge resim veya desteklenmeyen formatta olabilir)."
                )

            text = re.sub(r"[ \t\u00a0]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
            total_pages = max(1, math.ceil(len(text) / self.DOCUMENT_MARKDOWN_CHUNK_SIZE))
            current = max(1, min(page_number, total_pages))
            start = (current - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE
            chunk = text[start:start + self.DOCUMENT_MARKDOWN_CHUNK_SIZE]

            return EpdkDocumentMarkdown(
                source_id=source_id, source_url=source_url,
                baslik=baslik, karar_no=karar_no, karar_tarihi=karar_tarihi,
                belge_turu=belge_turu,
                markdown_chunk=chunk, current_page=current,
                total_pages=total_pages, is_paginated=(total_pages > 1),
                error_message=None
            )

        except httpx.RequestError as e:
            err = str(e)
            logger.error(f"EpdkApiClient: HTTP error downloading {content_id}: {err}")
        except Exception as e:
            err = str(e)
            logger.error(f"EpdkApiClient: Unexpected error downloading {content_id}: {err}")
        return EpdkDocumentMarkdown(
            source_id=source_id, source_url=source_url,
            baslik=baslik, karar_no=karar_no, karar_tarihi=karar_tarihi,
            belge_turu=None,
            markdown_chunk=None, current_page=page_number,
            total_pages=0, is_paginated=False,
            error_message=f"Belge alınamadı: {err}"
        )

    async def close_client_session(self):
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("EpdkApiClient: HTTP client session closed.")