# spk_mcp_module/client.py

import asyncio
import io
import json
import logging
import math
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .models import (
    SpkSearchRequest,
    SpkDecisionSummary,
    SpkSearchResult,
    SpkBultenSummary,
    SpkDocumentMarkdown
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

class SpkApiClient:
    """
    API client for searching and retrieving Sermaye Piyasası Kurulu (SPK) documents:
    ilke kararları, rehberler, tebliğler/yönetmelikler and haftalık bültenler.
    Uses the official mevzuat.spk.gov.tr API and spk.gov.tr bülten listing.
    """

    API_BASE_URL = "https://mevzuat.spk.gov.tr/api"
    BULTEN_BASE_URL = "https://spk.gov.tr"
    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000

    CONTENT_SOURCE_BY_TUR = {
        "Kurul Kararı": "IlkeKarari",
        "İlke Kararı": "IlkeKarari",
        "Rehber": "Rehber",
        "Tebliğ": "Mevzuat",
        "Yönetmelik": "Mevzuat",
        "Kanun": "Mevzuat",
        "Diğer Karar": "Mevzuat",
        "Diğer": "Mevzuat",
    }

    def __init__(self, request_timeout: float = 60.0):
        self.http_client = httpx.AsyncClient(
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            timeout=request_timeout,
            verify=True,
            follow_redirects=True
        )

    def _to_summary(self, item: Dict[str, Any]) -> Optional[SpkDecisionSummary]:
        content_id = item.get("contentID")
        content_source = item.get("contentSource")
        if content_id is None:
            return None
        bulten_yili = item.get("bultenYili")
        bulten_no = item.get("bultenNo")
        ilgili_bulten = f"{bulten_yili}/{bulten_no}" if bulten_yili and bulten_no else None
        return SpkDecisionSummary(
            baslik=item.get("title"),
            tur=item.get("tur"),
            tarih=item.get("kurulKararTarih") or item.get("kurulKararTarihi"),
            toplanti_no=item.get("kurulToplantiNo"),
            resmi_gazete_tarihi=item.get("resmiGazeteTarih") or item.get("resmiGazeteTarihi"),
            resmi_gazete_sayisi=item.get("resmiGazeteSayi"),
            bulten_yili=bulten_yili,
            bulten_no=bulten_no,
            kisim=item.get("kisim"),
            bolum=item.get("bolum"),
            baglantili_tebligler=item.get("baglantiliTebliglerStr"),
            ilgili_konular=item.get("ilgiliKonularStr"),
            content_source=content_source,
            content_id=content_id,
            document_id=f"spk:{str(content_source).lower()}:{content_id}" if content_source else f"spk:doc:{content_id}",
            ilgili_bulten=ilgili_bulten,
            ilgili_bulten_document_id=f"spk:bulten:{ilgili_bulten}" if ilgili_bulten else None
        )

    def _matches_filters(self, item: Dict[str, Any], params: SpkSearchRequest) -> bool:
        if params.tur:
            expected_source = self.CONTENT_SOURCE_BY_TUR.get(params.tur)
            source = item.get("contentSource")
            if expected_source and source and source != expected_source:
                return False
            if not expected_source:
                tur = (item.get("tur") or "").strip()
                if params.tur not in tur and tur not in params.tur:
                    return False
        if params.bulten_yili is not None and item.get("bultenYili") != params.bulten_yili:
            return False
        if params.bulten_no is not None and item.get("bultenNo") != params.bulten_no:
            return False
        return True

    async def search_decisions(self, params: SpkSearchRequest) -> SpkSearchResult:
        """Search SPK documents via the official mevzuat.spk.gov.tr API."""
        logger.info(
            f"SpkApiClient: Searching with keywords={params.keywords!r}, "
            f"search_field={params.search_field!r}, tur={params.tur!r}"
        )

        raw_items: List[Dict[str, Any]] = []
        try:
            if params.keywords and params.keywords.strip():
                sf = params.search_field or "all"
                if sf not in ("all", "title", "content"):
                    sf = "all"
                response = await self.http_client.get(
                    f"{self.API_BASE_URL}/Search/{params.keywords.strip()}",
                    params={"sf": sf}
                )
                response.raise_for_status()
                data = response.json()
                raw_items = data if isinstance(data, list) else (data.get("data") or [])
            else:
                # Empty keywords -> detailed POST search with filters
                payload = {
                    "id": 0,
                    "searchField": params.search_field or "all",
                    "kisimId": None,
                    "bolumId": None,
                    "konuGrubuId": None,
                    "konuId": None,
                    "rgDateBegin": None,
                    "rgDateEnd": None,
                    "kkDateBegin": None,
                    "kkDateEnd": None,
                    "type": params.tur or None,
                    "keywords": "",
                }
                response = await self.http_client.post(
                    f"{self.API_BASE_URL}/Search",
                    json=payload
                )
                response.raise_for_status()
                data = response.json()
                raw_items = data if isinstance(data, list) else (data.get("data") or [])
        except httpx.RequestError as e:
            logger.error(f"SpkApiClient: HTTP request error during search: {e}")
        except Exception as e:
            logger.error(f"SpkApiClient: Unexpected error during search: {e}")

        matched = [item for item in raw_items if self._matches_filters(item, params)]
        summaries = [s for s in (self._to_summary(item) for item in matched) if s is not None]

        total = len(summaries)
        start = (params.page - 1) * params.pageSize
        page_items = summaries[start:start + params.pageSize]

        logger.info(f"SpkApiClient: total={total}, returning {len(page_items)} on page {params.page}")
        return SpkSearchResult(
            decisions=page_items,
            total_results=total,
            page=params.page,
            pageSize=params.pageSize,
            query=params.keywords
        )

    async def list_bultenler(self, yil: int) -> List[SpkBultenSummary]:
        """List SPK haftalık bültenleri for a given year from spk.gov.tr."""
        logger.info(f"SpkApiClient: Listing bültenler for year {yil}")
        try:
            url = f"{self.BULTEN_BASE_URL}/spk-bultenleri/{yil}-yili-spk-bultenleri"
            response = await self.http_client.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            results: List[SpkBultenSummary] = []
            seen: set = set()

            anchors = soup.find_all("a", href=True)
            for a in anchors:
                href = a["href"]
                if ".pdf" not in href.lower():
                    continue
                pdf_url = urljoin(self.BULTEN_BASE_URL, href)
                if pdf_url in seen:
                    continue
                seen.add(pdf_url)

                no_match = re.search(rf"{yil}-(\d{{1,3}})\.pdf", pdf_url)
                if not no_match:
                    no_match = re.search(rf"{yil}/(\d{{1,3}})", a.get_text(" ", strip=True))
                if not no_match:
                    continue
                bulten_no = no_match.group(1)

                li = a
                while li is not None and li.name != "li" and li.parent is not None:
                    li = li.parent
                container = li if li is not None and li.name == "li" else a

                baslik = re.sub(r"\s+", " ", container.get_text(" ", strip=True))[:300]

                tarih_match = re.search(r"(\d{1,2} [A-Za-zğüşöçıİĞÜŞÖÇ]+ 20\d{2})", baslik)
                tarih = tarih_match.group(1) if tarih_match else None

                no_str = f"{yil}/{bulten_no}"
                results.append(SpkBultenSummary(
                    no=no_str,
                    yil=yil,
                    tarih=tarih,
                    baslik=baslik,
                    pdf_url=pdf_url,
                    document_id=f"spk:bulten:{no_str}"
                ))

            results.sort(key=lambda b: b.no or "", reverse=True)
            logger.info(f"SpkApiClient: Found {len(results)} bültenler for {yil}")
            return results
        except Exception as e:
            logger.error(f"SpkApiClient: Failed to list bültenler for {yil}: {e}")
            return []

    @staticmethod
    def _pdf_to_text(pdf_bytes: bytes) -> str:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        return "\n\n".join(pages).strip()

    async def get_document(self, document_id: str, page_number: int = 1) -> SpkDocumentMarkdown:
        """Retrieve an SPK document (ilke kararı/rehber/mevzuat/bülten) as paginated Markdown."""
        logger.info(f"SpkApiClient: Getting document {document_id}, page={page_number}")

        parts = document_id.split(":")
        if len(parts) < 3 or parts[0] != "spk":
            return self._error_result(document_id, f"Geçersiz belge kimliği: {document_id}")
        kind, identifier = parts[1], ":".join(parts[2:])

        try:
            if kind in ("ilke", "rehber", "mevzuat"):
                if not identifier.isdigit():
                    return self._error_result(document_id, f"Geçersiz içerik kimliği: {identifier}")
                file_endpoint = {
                    "ilke": "IlkeKarari/File",
                    "rehber": "Rehber/File",
                    "mevzuat": "Mevzuat/File",
                }[kind]
                pdf_url = f"{self.API_BASE_URL}/{file_endpoint}/{identifier}"
                pdf_bytes = await self._download_pdf(pdf_url)
            elif kind == "bulten":
                yil_no = identifier.split("/")
                if len(yil_no) != 2 or not yil_no[0].isdigit():
                    return self._error_result(document_id, f"Geçersiz bülten kimliği: {identifier}")
                yil, no = int(yil_no[0]), yil_no[1]
                pdf_url = await self._find_bulten_pdf_url(yil, no)
                if not pdf_url:
                    return self._error_result(document_id, f"{yil} yılı {no} numaralı bülten bulunamadı.")
                pdf_bytes = await self._download_pdf(pdf_url)
            else:
                return self._error_result(document_id, f"Bilinmeyen belge türü: {kind}")

            full_text = await asyncio.to_thread(self._pdf_to_text, pdf_bytes)
            if not full_text.strip():
                return self._error_result(document_id, "PDF metni boş (tarama içermiyor olabilir).")

            total_pages = max(1, math.ceil(len(full_text) / self.DOCUMENT_MARKDOWN_CHUNK_SIZE))
            current = max(1, min(page_number, total_pages))
            start = (current - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE
            chunk = full_text[start:start + self.DOCUMENT_MARKDOWN_CHUNK_SIZE]

            return SpkDocumentMarkdown(
                source_id=document_id,
                source_url=pdf_url,
                baslik=None,
                tur=kind,
                tarih=None,
                toplanti_no=None,
                markdown_chunk=chunk,
                current_page=current,
                total_pages=total_pages,
                is_paginated=(total_pages > 1),
                error_message=None
            )
        except Exception as e:
            logger.error(f"SpkApiClient: Failed to get document {document_id}: {e}")
            return self._error_result(document_id, f"Belge alınamadı: {str(e)}")

    async def _download_pdf(self, pdf_url: str) -> bytes:
        response = await self.http_client.get(pdf_url)
        response.raise_for_status()
        if not response.content:
            raise ValueError("Boş PDF yanıtı")
        return response.content

    async def _find_bulten_pdf_url(self, yil: int, no: str) -> Optional[str]:
        bultenler = await self.list_bultenler(yil)
        for b in bultenler:
            if b.no == f"{yil}/{no}":
                return b.pdf_url
        for b in bultenler:
            if b.pdf_url and re.search(rf"{yil}-0*{no}\.pdf", b.pdf_url):
                return b.pdf_url
        return None

    def _error_result(self, document_id: str, message: str) -> SpkDocumentMarkdown:
        return SpkDocumentMarkdown(
            source_id=document_id,
            source_url=None,
            baslik=None,
            tur=None,
            tarih=None,
            toplanti_no=None,
            markdown_chunk=None,
            current_page=1,
            total_pages=0,
            is_paginated=False,
            error_message=message
        )

    async def close_client_session(self):
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("SpkApiClient: HTTP client session closed.")