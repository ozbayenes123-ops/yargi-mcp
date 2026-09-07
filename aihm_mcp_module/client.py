# aihm_mcp_module/client.py

import asyncio
import logging
import math
from typing import Any, Dict, List, Optional

import httpx

from .models import (
    AihmDecisionSummary,
    AihmDocumentMarkdown,
    AihmSearchRequest,
    AihmSearchResult,
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


class AihmApiClient:
    """
    Client for the European Court of Human Rights HUDOC case-law database.

    The public query API is a GET endpoint:
        https://hudoc.echr.coe.int/app/query/results
    with Solr-style query parameters (query, select, sort, start, length,
    rankingModelId). Full text is served from:
        https://hudoc.echr.coe.int/app/conversion/docx/html/body?library=ECHR&id={itemid}
    """

    QUERY_URL = "https://hudoc.echr.coe.int/app/query/results"
    CONTENT_URL = "https://hudoc.echr.coe.int/app/conversion"
    LIBRARY = "ECHR"
    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000

    SELECT_FIELDS = (
        "itemid,docname,doctype,typedescription,application,appno,conclusion,"
        "importance,originatingbody,kpdate,kpdateAsText,documentcollectionid,"
        "documentcollectionid2,languageisocode,isplaceholder,doctypebranch,"
        "respondent,ecli"
    )

    def __init__(self, request_timeout: float = 60.0):
        self.http_client = httpx.AsyncClient(
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            timeout=request_timeout,
            verify=True,
            follow_redirects=True,
        )

    def _build_query(self, params: AihmSearchRequest) -> str:
        """Build the Solr-style HUDOC query string from request parameters."""
        parts = ['contentsitename:"ECHR"']

        lang = params.language.strip() or "ENG"
        parts.append(f'(languageisocode:"{lang}")')

        if params.keywords.strip():
            kw = params.keywords.strip().replace('"', "")
            parts.append(f"(text:({kw}))")

        if params.docname.strip():
            parts.append(f'(docname:"{params.docname.strip().replace(chr(34), "")}")')

        if params.appno.strip():
            parts.append(f'(appno:"{params.appno.strip().replace(chr(34), "")}")')

        if params.ecli.strip():
            parts.append(f'(ecli:"{params.ecli.strip().replace(chr(34), "")}")')

        if params.collection.strip():
            parts.append(f"(documentcollectionid2:{params.collection.strip()})")

        if params.importance > 0:
            parts.append(f"(importance:{params.importance})")

        if params.date_from.strip():
            parts.append(f"(kpdate:[{params.date_from.strip()}T00:00:00.000Z TO *])")
        if params.date_to.strip():
            parts.append(f"(kpdate:[* TO {params.date_to.strip()}T23:59:59.999Z])")

        return " AND ".join(parts)

    async def search_cases(self, params: AihmSearchRequest) -> AihmSearchResult:
        """Search ECHR case law via GET /app/query/results."""
        query = self._build_query(params)
        request_params: Dict[str, Any] = {
            "query": query,
            "select": self.SELECT_FIELDS,
            "sort": "",
            "start": (params.page - 1) * params.pageSize,
            "length": params.pageSize,
            "rankingModelId": "11111111-0000-0000-0000-000000000000",
        }
        logger.info("AihmApiClient: query=%s", query)

        try:
            response = await self.http_client.get(self.QUERY_URL, params=request_params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            logger.error("AihmApiClient: HTTP %s on HUDOC query", e.response.status_code)
            return AihmSearchResult(results=[], total_results=0, page=params.page, pageSize=params.pageSize)
        except Exception as e:
            logger.error("AihmApiClient: query error: %s", e)
            return AihmSearchResult(results=[], total_results=0, page=params.page, pageSize=params.pageSize)

        records = data.get("results") or []
        results: List[AihmDecisionSummary] = []
        for rec in records:
            c = rec.get("columns") or {}
            results.append(
                AihmDecisionSummary(
                    itemid=str(c.get("itemid", "")),
                    docname=str(c.get("docname", "")),
                    appno=c.get("appno"),
                    doctype=c.get("doctype"),
                    typedescription=c.get("typedescription"),
                    originatingbody=c.get("originatingbody"),
                    kpdate=c.get("kpdate"),
                    languageisocode=c.get("languageisocode"),
                    documentcollectionid2=c.get("documentcollectionid2"),
                    importance=c.get("importance"),
                    conclusion=c.get("conclusion"),
                    ecli=c.get("ecli"),
                    respondent=c.get("respondent"),
                    is_placeholder=str(c.get("isplaceholder", "")).lower() == "true",
                )
            )

        return AihmSearchResult(
            results=results,
            total_results=int(data.get("resultcount") or 0),
            page=params.page,
            pageSize=params.pageSize,
        )

    async def get_document_html(self, itemid: str) -> Optional[str]:
        """Fetch the full HTML text of a case document."""
        url = f"{self.CONTENT_URL}/docx/html/body?library={self.LIBRARY}&id={itemid}"
        try:
            response = await self.http_client.get(url)
            if response.status_code == 204:
                logger.warning("AihmApiClient: no content for itemid=%s", itemid)
                return None
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.error("AihmApiClient: document fetch error for %s: %s", itemid, e)
            return None

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Strip HTML tags from the document body."""
        import re as _re

        text = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = _re.sub(r"(?is)<br[^>]*>", "\n", text)
        text = _re.sub(r"(?is)</(p|div|h[1-6]|li|tr)>", "\n", text)
        text = _re.sub(r"(?is)<[^>]+>", " ", text)
        text = _re.sub(r"[ \t\u00a0]+", " ", text)
        text = _re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()

    async def get_document_markdown(self, itemid: str, page_number: int = 1) -> AihmDocumentMarkdown:
        """Retrieve a case document as paginated Markdown."""
        logger.info("AihmApiClient: getting document for itemid=%s, page=%s", itemid, page_number)
        html = await self.get_document_html(itemid)
        if not html:
            return AihmDocumentMarkdown(
                itemid=itemid,
                docname="",
                markdown_chunk=None,
                current_page=page_number or 1,
                total_pages=0,
                is_paginated=False,
                error_message="Could not retrieve document content (record may be a placeholder).",
            )

        text = await asyncio.to_thread(self._html_to_text, html)
        if not text:
            return AihmDocumentMarkdown(
                itemid=itemid,
                docname="",
                markdown_chunk=None,
                current_page=page_number or 1,
                total_pages=0,
                is_paginated=False,
                error_message="Document content is empty.",
            )

        total_pages = max(1, math.ceil(len(text) / self.DOCUMENT_MARKDOWN_CHUNK_SIZE))
        current_page_clamped = max(1, min(page_number or 1, total_pages))
        start = (current_page_clamped - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE
        chunk = text[start : start + self.DOCUMENT_MARKDOWN_CHUNK_SIZE]

        return AihmDocumentMarkdown(
            itemid=itemid,
            docname="",
            markdown_chunk=chunk,
            current_page=current_page_clamped,
            total_pages=total_pages,
            is_paginated=(total_pages > 1),
            error_message=None,
        )

    async def close_client_session(self):
        """Close the HTTP client session."""
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("AihmApiClient: HTTP client session closed.")
