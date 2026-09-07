# kdk_mcp_module/client.py

import asyncio
import io
import json
import logging
import math
import re
import ssl
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from .models import (
    KdkSearchRequest,
    KdkDecisionSummary,
    KdkSearchResult,
    KdkDocumentMarkdown
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

class KdkApiClient:
    """
    API client for searching and retrieving Kamu Denetçiliği Kurumu (Ombudsmanlık)
    decisions directly from the official "Kararlar Bilgi Bankası" web application.
    """

    BASE_URL = "https://kararlar.ombudsman.gov.tr"
    SEARCH_ENDPOINT = "/Arama/IndexPaging"
    DETAIL_ENDPOINT = "/Arama/Detay"
    DOWNLOAD_ENDPOINT = "/Arama/Download"
    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000

    def __init__(self, request_timeout: float = 60.0):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ssl_context.options |= ssl.OP_LEGACY_SERVER_CONNECT
        ssl_context.set_ciphers("ALL:!aNULL:!eNULL:!EXPORT:!DES:!RC4:!MD5:!PSK:!SRP:!CAMELLIA")
        self.http_client = httpx.AsyncClient(
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "X-Requested-With": "XMLHttpRequest"
            },
            timeout=request_timeout,
            verify=ssl_context,
            follow_redirects=True
        )

    @staticmethod
    def _decode_response_bytes(content: bytes) -> str:
        """Decode response bytes handling Turkish character encodings robustly."""
        for enc in ("utf-8", "windows-1254", "iso-8859-9"):
            try:
                return content.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return content.decode("utf-8", errors="replace")

    def _parse_json(self, content: bytes) -> Dict[str, Any]:
        text = self._decode_response_bytes(content)
        return json.loads(text)

    async def search_decisions(self, params: KdkSearchRequest) -> KdkSearchResult:
        """Search KDK decisions via the DataTables server-side endpoint."""
        logger.info(f"KdkApiClient: Searching decisions with keywords={params.keywords!r}, karar_turu={params.karar_turu!r}")

        start = (params.page - 1) * params.pageSize

        form_data = {
            "draw": "1",
            "start": str(start),
            "length": str(params.pageSize),
            "order[0][column]": "0",
            "order[0][dir]": "desc",
            "basbas": "",
            "basbit": "",
            "basnoyil": "",
            "basnosayi": "",
            "sikayetkonu": params.sikayet_konu or "",
            "konuidarekelimeleri": "",
            "karbas": "",
            "karbit": "",
            "kararTuru": params.karar_turu or "",
            "evrakkonu": params.keywords or "",
            "kararkelimeleri": "",
            "ortakalankelimeleri": "",
        }

        try:
            response = await self.http_client.post(
                self.BASE_URL + self.SEARCH_ENDPOINT,
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
            )
            response.raise_for_status()
            data = self._parse_json(response.content)

            decisions: List[KdkDecisionSummary] = []
            raw_items = data.get("data") or []
            for item in raw_items:
                evrak_id = item.get("evraK_ID")
                decisions.append(KdkDecisionSummary(
                    karar_no=item.get("karaR_NO"),
                    basvuru_no=item.get("sikayeT_NO"),
                    karar_tarihi=item.get("karaR_TARIH"),
                    karar_turu=item.get("karaR_TURU"),
                    konu=item.get("evraK_KONU"),
                    idare=item.get("sikayeT_EDILEN_KURUM"),
                    sikayet_konu=item.get("sikayeT_KONU"),
                    sikayet_alt_konu=item.get("sikayeT_ALT_KONU"),
                    ozet=item.get("kbB_OZET"),
                    evrak_id=evrak_id,
                    yayin_url=item.get("karaR_YAYIN_URL"),
                    document_id=f"kdk:{evrak_id}" if evrak_id is not None else None
                ))

            total = data.get("recordsFiltered") or 0
            logger.info(f"KdkApiClient: Found {len(decisions)} decisions on page {params.page}, total={total}")
            return KdkSearchResult(
                decisions=decisions,
                total_results=int(total),
                page=params.page,
                pageSize=params.pageSize,
                query=params.keywords
            )

        except httpx.RequestError as e:
            logger.error(f"KdkApiClient: HTTP request error during search: {e}")
        except Exception as e:
            logger.error(f"KdkApiClient: Unexpected error during search: {e}")
        return KdkSearchResult(
            decisions=[], total_results=0, page=params.page,
            pageSize=params.pageSize, query=params.keywords
        )

    @staticmethod
    def _html_decision_to_text(html: str) -> str:
        """Convert the KDK decision detail HTML into plain text."""
        soup = BeautifulSoup(html or "", "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        text = soup.get_text()
        text = re.sub(r"[ \t\u00a0]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _pdf_to_text(pdf_bytes: bytes) -> str:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        return "\n\n".join(pages).strip()

    async def get_decision_document(
        self,
        evrak_id: int,
        page_number: int = 1,
        pdf_url: Optional[str] = None,
        tarih: Optional[str] = None
    ) -> KdkDocumentMarkdown:
        """Retrieve a KDK decision full text as paginated Markdown (HTML detail, PDF fallback)."""
        logger.info(f"KdkApiClient: Getting decision document evrak_id={evrak_id}, page={page_number}")

        source_id = f"kdk:{evrak_id}"
        source_url = f"{self.BASE_URL}/Arama/Detay?id={evrak_id}"

        # 1) Try the HTML detail endpoint (already OCR'd text, when available)
        try:
            resp = await self.http_client.get(
                self.BASE_URL + self.DETAIL_ENDPOINT,
                params={"id": str(evrak_id), "kelimeler": ""}
            )
            resp.raise_for_status()
            data = self._parse_json(resp.content)
            raw_metin = data.get("metin") or ""
            if raw_metin and "tamamlanmamıştır" not in raw_metin.lower():
                full_text = self._html_decision_to_text(raw_metin)
                if full_text.strip():
                    return self._paginate_markdown(
                        full_text, source_id, source_url,
                        page_number=page_number, meta={}
                    )
                logger.info("KdkApiClient: HTML detail was empty, falling back to PDF")
        except Exception as e:
            logger.warning(f"KdkApiClient: HTML detail failed ({e}), falling back to PDF")

        # 2) PDF fallback (Download endpoint) when a published URL is known
        if pdf_url:
            try:
                pdf_resp = await self.http_client.get(
                    self.BASE_URL + self.DOWNLOAD_ENDPOINT,
                    params={"url": pdf_url, "tarih": tarih or ""}
                )
                pdf_resp.raise_for_status()
                pdf_text = await asyncio.to_thread(self._pdf_to_text, pdf_resp.content)
                if pdf_text.strip():
                    return self._paginate_markdown(
                        pdf_text, source_id, source_url,
                        page_number=page_number, meta={}
                    )
            except Exception as e:
                logger.error(f"KdkApiClient: PDF fallback failed for {evrak_id}: {e}")

        return KdkDocumentMarkdown(
            source_id=source_id,
            source_url=source_url,
            karar_no=None,
            basvuru_no=None,
            karar_turu=None,
            idare=None,
            konu=None,
            markdown_chunk=None,
            current_page=page_number,
            total_pages=0,
            is_paginated=False,
            error_message="Karar metni alınamadı (OCR taraması tamamlanmamış ve PDF indirilemedi)."
        )

    def _paginate_markdown(
        self,
        full_text: str,
        source_id: str,
        source_url: str,
        page_number: int,
        meta: Dict[str, Optional[str]]
    ) -> KdkDocumentMarkdown:
        content_length = len(full_text)
        total_pages = max(1, math.ceil(content_length / self.DOCUMENT_MARKDOWN_CHUNK_SIZE))
        current = max(1, min(page_number, total_pages))
        start = (current - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE
        chunk = full_text[start:start + self.DOCUMENT_MARKDOWN_CHUNK_SIZE]

        return KdkDocumentMarkdown(
            source_id=source_id,
            source_url=source_url,
            karar_no=meta.get("karar_no"),
            basvuru_no=meta.get("basvuru_no"),
            karar_turu=meta.get("karar_turu"),
            idare=meta.get("idare"),
            konu=meta.get("konu"),
            markdown_chunk=chunk,
            current_page=current,
            total_pages=total_pages,
            is_paginated=(total_pages > 1),
            error_message=None
        )

    async def close_client_session(self):
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("KdkApiClient: HTTP client session closed.")