# hsk_mcp_module/client.py

import html
import io
import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pypdf import PdfReader

from .models import (
    HskSearchRequest,
    HskDecisionSummary,
    HskSearchResult,
    HskDocumentMarkdown
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

LIST_URL = "https://www.hsk.gov.tr/disiplin-kararlari"
PDF_URL = "https://www.hsk.gov.tr/Eklentiler/Dosyalar"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
SPLIT_MARKER = "İKİNCİ DAİRE KARARI"

MADDE_FILTERS = {
    "uyarma": "UYARMA",
    "ayliktan_kesme": "AYLIKTAN KESME",
    "kinama": "KINAMA",
    "kademe_ilerlemesi_durdurma": "KADEME İLERLEMESİNİ DURDURMA",
    "derece_yukselmesi_durdurma": "DERECE YÜKSELMESİNİ DURDURMA",
    "yer_degistirme": "YER DEĞİŞTİRME",
    "meslekten_cikarma": "MESLEKTEN ÇIKARMA",
    "ceza_tayinine_yer_olmadigi": "CEZA TAYİNİNE YER OLMADIĞI",
    "islemden_kaldirma": "İŞLEMDEN KALDIRMA",
}


def _fold(text: str) -> str:
    """Turkish-diacritic-insensitive fold."""
    if not text:
        return ""
    t = text.lower()
    t = t.replace("\u0131", "i")
    t = t.replace("i\u0307", "i")
    t = t.replace("\u015f", "s").replace("\u00e7", "c")
    t = t.replace("\u00f6", "o").replace("\u00fc", "u")
    t = t.replace("\u011f", "g").replace("\u00e2", "a")
    t = t.replace("\u00ee", "i").replace("\u00fb", "u")
    return t


def _strip_tags(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


class HskApiClient:
    """
    API client for searching HSK (Hâkimler ve Savcılar Kurulu) İkinci Daire
    disiplin kararları directly from hsk.gov.tr:
      - Parses the malformed (unclosed-tag) disiplin-kararlari page into
        madde (UYARMA/KINAMA/...) rows with per-fıkra PDF links
      - Downloads each PDF once, extracts text via pypdf, caches to disk
      - Splits multi-decision PDFs on "İKİNCİ DAİRE KARARI"
      - Searches locally with diacritic-insensitive AND matching
    """

    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000
    PDF_TEXT_TTL_SECONDS = 30 * 24 * 3600  # kararlar statiktir; 30 gün

    def __init__(self, request_timeout: float = 60.0):
        self.http_client = httpx.AsyncClient(
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            timeout=request_timeout,
            follow_redirects=True
        )
        self._madde_index: Optional[List[Dict[str, Any]]] = None
        self._madde_index_fetched_at: Optional[float] = None

    # ---------- page parsing ----------

    @staticmethod
    def _parse_madde_blocks(content: str) -> List[Dict[str, Any]]:
        """Parse the malformed HSK page into madde blocks with per-fıkra PDFs."""
        start = content.find("<table")
        end = content.find("</table>")
        if start == -1 or end == -1:
            return []
        table = content[start:end]

        blocks: List[Dict[str, Any]] = []
        for block in re.split(r"<hr>", table):
            if "<tr" not in block:
                continue
            rows = block.split("<tr")[1:]
            madde_row = next((r for r in rows if "rowspan=2" in r), None)
            if madde_row is None:
                continue

            # madde title from the rowspan cell
            title_cell = madde_row.split("<td", 1)[1] if "<td" in madde_row else ""
            title = _strip_tags(title_cell)
            title = re.sub(r"\s+", " ", title).strip()
            if not title:
                continue

            # fıkra codes row (the row right after the madde row)
            codes_row = rows[1] if len(rows) > 1 else ""
            fikralar: List[Dict[str, str]] = []
            if codes_row:
                cells = codes_row.split("<td")[1:]
                for cell in cells:
                    pdf_m = re.search(r"/Eklentiler/Dosyalar/([0-9a-fA-F-]+)\.pdf", cell)
                    if not pdf_m:
                        continue
                    code = _strip_tags(cell)
                    if not code:
                        code = "tümü"
                    fikralar.append({"fikra": code, "uuid": pdf_m.group(1)})

            if not fikralar:
                continue
            blocks.append({"madde": title, "fikralar": fikralar})
        return blocks

    async def _get_madde_index(self) -> List[Dict[str, Any]]:
        """Cached parse of the HSK disiplin-kararlari page."""
        now = datetime.now().timestamp()
        if self._madde_index is not None and (now - (self._madde_index_fetched_at or 0)) < 3600:
            return self._madde_index
        response = await self.http_client.get(LIST_URL)
        response.raise_for_status()
        self._madde_index = self._parse_madde_blocks(response.text)
        self._madde_index_fetched_at = now
        logger.info(f"HskApiClient: parsed {len(self._madde_index)} madde blocks")
        return self._madde_index

    # ---------- PDF text ----------

    def _pdf_text_path(self, uuid: str) -> Path:
        return CACHE_DIR / f"hsk_{uuid}.txt"

    def _read_pdf_text(self, uuid: str) -> Optional[str]:
        try:
            path = self._pdf_text_path(uuid)
            if not path.exists():
                return None
            stat = path.stat()
            if (datetime.now().timestamp() - stat.st_mtime) > self.PDF_TEXT_TTL_SECONDS:
                return None
            return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"HskApiClient: pdf text cache read failed {uuid}: {e}")
            return None

    def _write_pdf_text(self, uuid: str, text: str):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._pdf_text_path(uuid).write_text(text, encoding="utf-8")
        except Exception as e:
            logger.warning(f"HskApiClient: pdf text cache write failed {uuid}: {e}")

    async def _get_pdf_text(self, uuid: str) -> str:
        """Download + extract PDF text once, then serve from disk cache."""
        cached = self._read_pdf_text(uuid)
        if cached is not None:
            return cached
        url = f"{PDF_URL}/{uuid}.pdf"
        response = await self.http_client.get(url)
        response.raise_for_status()
        reader = PdfReader(io.BytesIO(response.content))
        pages = [pg.extract_text() or "" for pg in reader.pages]
        text = "\n\n".join(pages)
        text = re.sub(r"[ \t\u00a0]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        self._write_pdf_text(uuid, text)
        return text

    # ---------- search ----------

    async def search_decisions(self, params: HskSearchRequest) -> HskSearchResult:
        """Search HSK İkinci Daire disiplin kararları (diacritic-insensitive AND)."""
        logger.info(f"HskApiClient: searching keywords={params.keywords!r}, madde={params.madde}")
        try:
            index = await self._get_madde_index()
        except Exception as e:
            logger.error(f"HskApiClient: madde index failed: {e}")
            return HskSearchResult(
                decisions=[], total_results=0, page=params.page,
                pageSize=params.pageSize, query=params.keywords,
                taranan_pdf=0
            )

        madde_filter = None
        if params.madde:
            madde_filter = _fold(MADDE_FILTERS.get(params.madde, params.madde))

        tokens = [_fold(t) for t in (params.keywords or "").split() if t.strip()]
        matched: List[HskDecisionSummary] = []
        pdf_count = 0

        for block in index:
            title = block["madde"]
            if madde_filter and madde_filter not in _fold(title):
                continue
            for f in block["fikralar"]:
                uuid = f["uuid"]
                try:
                    text = await self._get_pdf_text(uuid)
                except Exception as e:
                    logger.warning(f"HskApiClient: pdf {uuid} failed: {e}")
                    continue
                pdf_count += 1
                parts = [p.strip() for p in re.split(SPLIT_MARKER, text) if p.strip()]
                for sira, part in enumerate(parts, start=1):
                    folded = _fold(part)
                    if tokens and not all(t in folded for t in tokens):
                        continue
                    ozet = re.sub(r"\s+", " ", part)[:250]
                    matched.append(HskDecisionSummary(
                        madde=title,
                        fikra=f["fikra"],
                        esas_no=".....",  # kaynak anonimdir
                        karar_no=".....",
                        ozet=ozet,
                        uuid=uuid,
                        sira=sira,
                        document_id=f"hsk:disiplin:{uuid}:{sira}"
                    ))

        matched.sort(key=lambda s: (s.madde or "", s.fikra or ""))
        total = len(matched)
        start = (params.page - 1) * params.pageSize
        page_items = matched[start:start + params.pageSize]

        return HskSearchResult(
            decisions=page_items,
            total_results=total,
            page=params.page,
            pageSize=params.pageSize,
            query=params.keywords,
            taranan_pdf=pdf_count,
            bilgi_notu="Kaynak (hsk.gov.tr) kararları tamamen anonimleştirmiştir: esas/karar numarası ve tarih '.....' olarak görünür, karar yılına göre filtreleme yapılamaz."
        )

    # ---------- document ----------

    async def get_document(
        self,
        uuid: str,
        sira: int = 1,
        madde: Optional[str] = None,
        fikra: Optional[str] = None,
        page_number: int = 1
    ) -> HskDocumentMarkdown:
        """Return a single HSK disiplin kararı from its PDF as paginated Markdown."""
        source_id = f"hsk:disiplin:{uuid}:{sira}"
        source_url = f"{PDF_URL}/{uuid}.pdf"
        logger.info(f"HskApiClient: getting document {source_id}")

        try:
            text = await self._get_pdf_text(uuid)
        except Exception as e:
            logger.error(f"HskApiClient: pdf {uuid} failed: {e}")
            return HskDocumentMarkdown(
                source_id=source_id, source_url=source_url,
                madde=madde, fikra=fikra,
                markdown_chunk=None, current_page=page_number,
                total_pages=0, is_paginated=False,
                error_message=f"PDF alınamadı: {e}"
            )

        parts = [p.strip() for p in re.split(SPLIT_MARKER, text) if p.strip()]
        if sira > len(parts):
            return HskDocumentMarkdown(
                source_id=source_id, source_url=source_url,
                madde=madde, fikra=fikra,
                markdown_chunk=None, current_page=page_number,
                total_pages=0, is_paginated=False,
                error_message=f"PDF'de {len(parts)} karar var; istenen sıra {sira} geçersiz."
            )
        body = parts[sira - 1]

        total_pages = max(1, math.ceil(len(body) / self.DOCUMENT_MARKDOWN_CHUNK_SIZE))
        current = max(1, min(page_number, total_pages))
        start = (current - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE
        chunk = body[start:start + self.DOCUMENT_MARKDOWN_CHUNK_SIZE]

        return HskDocumentMarkdown(
            source_id=source_id, source_url=source_url,
            madde=madde, fikra=fikra,
            markdown_chunk=chunk, current_page=current,
            total_pages=total_pages, is_paginated=(total_pages > 1),
            error_message=None
        )

    async def close_client_session(self):
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("HskApiClient: HTTP client session closed.")