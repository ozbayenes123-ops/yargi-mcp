# mevzuat_mcp_module/client.py

import asyncio
import base64
import io
import logging
import math
import re
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from .models import (
    MevzuatDocumentMarkdown,
    MevzuatInTextMatch,
    MevzuatSearchRequest,
    MevzuatSearchResult,
    MevzuatSummary,
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


class MevzuatApiClient:
    """
    Client for the Adalet Bakanligi Bedesten legislation (Mevzuat) API.

    Keyless JSON REST API served at https://bedesten.adalet.gov.tr/mevzuat,
    backend of the https://mevzuat.adalet.gov.tr search portal. Responses use
    an envelope: {"data": ..., "metadata": {"FMTY": "SUCCESS"|"ERROR", ...}}.
    """

    BASE_URL = "https://bedesten.adalet.gov.tr/mevzuat"
    APPLICATION_NAME = "UyapMevzuat"
    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000

    def __init__(self, request_timeout: float = 60.0):
        self.http_client = httpx.AsyncClient(
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "AdaletApplicationName": self.APPLICATION_NAME,
                "Origin": "https://mevzuat.adalet.gov.tr",
                "Referer": "https://mevzuat.adalet.gov.tr/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=request_timeout,
            verify=True,
            follow_redirects=True,
        )

    async def _post_json(self, path: str, data: dict) -> Optional[Dict[str, Any]]:
        """POST an envelope-wrapped JSON request and return the response JSON, or None on failure."""
        try:
            payload = {"data": data, "applicationName": self.APPLICATION_NAME}
            response = await self.http_client.post(self.BASE_URL + path, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error("MevzuatApiClient: HTTP %s on %s", e.response.status_code, path)
            return None
        except Exception as e:
            logger.error("MevzuatApiClient: request error %s: %s", path, e)
            return None

    @staticmethod
    def _successful(envelope: Optional[Dict[str, Any]]) -> bool:
        if not envelope:
            return False
        return envelope.get("metadata", {}).get("FMTY") == "SUCCESS"

    async def search_mevzuat(self, params: MevzuatSearchRequest) -> MevzuatSearchResult:
        """Search legislation records via POST /searchDocuments."""
        sort_fields = ["RESMI_GAZETE_TARIHI"]
        sort_direction = "desc"
        data: Dict[str, Any] = {
            "pageSize": params.pageSize,
            "pageNumber": params.page,
            "sortFields": sort_fields,
            "sortDirection": sort_direction,
        }
        if params.mevzuatAdi.strip():
            data["mevzuatAdi"] = params.mevzuatAdi.strip()
        if params.mevzuatNo.strip():
            data["mevzuatNo"] = params.mevzuatNo.strip()
        if params.mevzuatTurList:
            data["mevzuatTurList"] = params.mevzuatTurList
        if params.resmiGazeteTarihiStart.strip():
            data["resmiGazeteTarihiStart"] = params.resmiGazeteTarihiStart.strip()
        if params.resmiGazeteTarihiEnd.strip():
            data["resmiGazeteTarihiEnd"] = params.resmiGazeteTarihiEnd.strip()

        envelope = await self._post_json("/searchDocuments", data)
        if not self._successful(envelope):
            logger.warning("MevzuatApiClient: searchDocuments failed: %s", envelope)
            return MevzuatSearchResult(
                results=[], total_results=0, page=params.page, pageSize=params.pageSize
            )

        inner = envelope.get("data") or {}
        records = inner.get("mevzuatList") or []
        total = inner.get("total") or 0

        results: List[MevzuatSummary] = []
        for rec in records:
            tur = rec.get("mevzuatTur") or {}
            results.append(
                MevzuatSummary(
                    mevzuat_id=str(rec.get("mevzuatId", "")),
                    mevzuat_no=rec.get("mevzuatNo"),
                    mevzuat_adi=str(rec.get("mevzuatAdi", "")),
                    mevzuat_tur=str(tur.get("name", "")),
                    mevzuat_tertip=rec.get("mevzuatTertip"),
                    resmi_gazete_tarihi=str(rec.get("resmiGazeteTarihi", "") or ""),
                    resmi_gazete_sayisi=str(rec.get("resmiGazeteSayisi", "") or ""),
                    url=rec.get("url"),
                    mukerrer=rec.get("mukerrer"),
                )
            )

        return MevzuatSearchResult(
            results=results,
            total_results=int(total or 0),
            page=params.page,
            pageSize=params.pageSize,
        )

    async def list_mevzuat_types(self) -> List[dict]:
        """Enumerate legislation types via POST /mevzuatTypes."""
        envelope = await self._post_json("/mevzuatTypes", {})
        if not self._successful(envelope):
            logger.warning("MevzuatApiClient: mevzuatTypes failed: %s", envelope)
            return []
        return envelope.get("data") or []

    async def get_madde_tree(self, mevzuat_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the legislation article tree via POST /mevzuatMaddeTree."""
        envelope = await self._post_json("/mevzuatMaddeTree", {"mevzuatId": mevzuat_id})
        if not self._successful(envelope):
            logger.warning("MevzuatApiClient: mevzuatMaddeTree failed: %s", envelope)
            return None
        return envelope.get("data") or {}

    async def get_mevzuat_content(self, mevzuat_id: str) -> Optional[str]:
        """
        Fetch and decode the full HTML text of a legislation via POST /getDocumentContent.

        Returns the decoded HTML string (Windows-1254) or None on failure.
        """
        envelope = await self._post_json(
            "/getDocumentContent", {"documentType": "MEVZUAT", "id": mevzuat_id}
        )
        if not self._successful(envelope):
            logger.warning("MevzuatApiClient: getDocumentContent failed: %s", envelope)
            return None

        inner = envelope.get("data") or {}
        content = inner.get("content")
        if not content:
            logger.warning("MevzuatApiClient: empty content for mevzuat_id=%s", mevzuat_id)
            return None

        try:
            raw = base64.b64decode(content)
            try:
                return raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return raw.decode("cp1254", errors="replace")
        except Exception as e:
            logger.error("MevzuatApiClient: content decode error: %s", e)
            return None

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Convert legislation HTML (Word-exported) to clean plain text."""
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "head", "meta", "link"]):
                tag.decompose()
            text = soup.get_text(separator="\n")
            text = re.sub(r"[ \t\u00a0]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n", text)
            return text.strip()
        except Exception as e:
            logger.error("MevzuatApiClient: html_to_text error: %s", e)
            return ""

    async def get_mevzuat_markdown(self, mevzuat_id: str, page_number: int = 1) -> MevzuatDocumentMarkdown:
        """Retrieve a legislation's full text as paginated Markdown."""
        logger.info("MevzuatApiClient: getting document content for mevzuat_id=%s, page=%s", mevzuat_id, page_number)
        html = await self.get_mevzuat_content(mevzuat_id)
        if not html:
            return MevzuatDocumentMarkdown(
                mevzuat_id=mevzuat_id,
                title="",
                markdown_chunk=None,
                current_page=page_number or 1,
                total_pages=0,
                is_paginated=False,
                error_message="Could not retrieve legislation content.",
            )

        text = await asyncio.to_thread(self._html_to_text, html)
        title = ""
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            first = text.splitlines()[0] if text.splitlines() else ""
            title = first[:200]

        if not text:
            return MevzuatDocumentMarkdown(
                mevzuat_id=mevzuat_id,
                title=title,
                markdown_chunk=None,
                current_page=page_number or 1,
                total_pages=0,
                is_paginated=False,
                error_message="Could not convert document content to text.",
            )

        content_length = len(text)
        total_pages = max(1, math.ceil(content_length / self.DOCUMENT_MARKDOWN_CHUNK_SIZE))
        current_page_clamped = max(1, min(page_number or 1, total_pages))
        start = (current_page_clamped - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE
        chunk = text[start : start + self.DOCUMENT_MARKDOWN_CHUNK_SIZE]

        return MevzuatDocumentMarkdown(
            mevzuat_id=mevzuat_id,
            title=title,
            markdown_chunk=chunk,
            current_page=current_page_clamped,
            total_pages=total_pages,
            is_paginated=(total_pages > 1),
            error_message=None,
        )

    async def search_within_mevzuat(
        self, mevzuat_id: str, keywords: str, max_matches: int = 10
    ) -> MevzuatInTextMatch:
        """Search a keyword inside a legislation's full text and return context snippets."""
        html = await self.get_mevzuat_content(mevzuat_id)
        if not html:
            return MevzuatInTextMatch(
                mevzuat_id=mevzuat_id,
                mevzuat_adi="",
                match_count=0,
                snippets=[],
            )

        text = await asyncio.to_thread(self._html_to_text, html)
        normalized_text = text.lower()
        normalized_keyword = keywords.strip().lower()
        snippets: List[str] = []
        matches = 0
        window = 150
        idx = 0
        while matches < max_matches:
            found = normalized_text.find(normalized_keyword, idx)
            if found == -1:
                break
            matches += 1
            start = max(0, found - window // 2)
            end = min(len(text), found + len(keywords) + window // 2)
            snippet = text[start:end].replace("\n", " ")
            snippets.append(snippet)
            idx = found + len(normalized_keyword)

        title = ""
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)

        return MevzuatInTextMatch(
            mevzuat_id=mevzuat_id,
            mevzuat_adi=title,
            match_count=matches,
            snippets=snippets,
        )

    async def close_client_session(self):
        """Close the HTTP client session."""
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("MevzuatApiClient: HTTP client session closed.")
