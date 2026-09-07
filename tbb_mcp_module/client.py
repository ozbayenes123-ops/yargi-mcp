# tbb_mcp_module/client.py

import html
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup

from .models import (
    TbbSearchRequest,
    TbbDecisionSummary,
    TbbSearchResult,
    TbbDocumentMarkdown
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

class TbbApiClient:
    """
    API client for searching and retrieving Türkiye Barolar Birliği (TBB)
    Disiplin Kurulu kararları directly from the official barobirlik.org.tr site.
    """

    BASE_URL = "https://www.barobirlik.org.tr"
    SEARCH_PATH = "/DisiplinKararlari"
    DETAIL_PATH = "/DisiplinKararlariDetay"
    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000

    def __init__(self, request_timeout: float = 120.0):
        self.http_client = httpx.AsyncClient(
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            timeout=request_timeout,
            follow_redirects=True
        )

    @staticmethod
    def _html_to_text(raw_html: str) -> str:
        """Decode HTML entities and strip tags, preserving paragraph breaks."""
        if not raw_html:
            return ""
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup.find_all(["p", "br"]):
            tag.append("\n")
        text = soup.get_text()
        text = html.unescape(text)
        text = re.sub(r"[ \t\u00a0]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    # --- TBB search workaround ---
    # The TBB site's own search breaks on ANY query containing ç/ö/ü/â/î/û:
    # it returns "Bulunamadı" for every such query (server-side encoding bug,
    # reproducible in a plain browser too). Other Turkish letters (ı, ş, ğ)
    # work fine, and the match itself is diacritic-SENSITIVE substring.
    # Workaround: query the site with the ç/ö/ü/â/î/û letters ASCII-folded,
    # fetch up to MAX_SITE_PAGES pages, then filter locally with a full
    # diacritic-insensitive fold so properly-spelled matches are found too.
    BREAKING_CHARS = "çöüâîû"
    MAX_SITE_PAGES = 10  # ~100 decisions scanned per search

    @staticmethod
    def _fold_broken(text: str) -> str:
        """Fold only the query-breaking letters to ASCII (ç->c, ö->o, ü->u, â->a, î->i, û->u)."""
        t = text.lower()
        for src, dst in [("ç", "c"), ("ö", "o"), ("ü", "u"), ("â", "a"), ("î", "i"), ("û", "u")]:
            t = t.replace(src, dst)
        return t

    @staticmethod
    def _fold_full(text: str) -> str:
        """Full Turkish diacritic fold for LOCAL filtering (data side)."""
        t = text.lower()
        t = t.replace("ç", "c").replace("ö", "o").replace("ü", "u")
        t = t.replace("â", "a").replace("î", "i").replace("û", "u")
        t = t.replace("ı", "i").replace("ş", "s").replace("ğ", "g")
        return t

    @staticmethod
    def _has_breaking(text: str) -> bool:
        low = text.lower()
        return any(c in low for c in TbbApiClient.BREAKING_CHARS)

    @classmethod
    def _parse_page(cls, content: str) -> Tuple[List[TbbDecisionSummary], Optional[int]]:
        """Parse one search-result page: decisions + total-pages estimate."""
        decisions: List[TbbDecisionSummary] = []
        soup = BeautifulSoup(content, "html.parser")
        for row in soup.select("tr[valign='top']"):
            spans = row.find_all("span", class_="text-info")
            tarih = esas = karar = None
            for span in spans:
                text = html.unescape(span.get_text(" ", strip=True))
                if text.startswith("T."):
                    tarih = text[2:].strip()
                elif text.startswith("E."):
                    esas = text[2:].strip()
                elif text.startswith("K."):
                    karar = text[2:].strip()
            paragraphs = [html.unescape(p.get_text(" ", strip=True)) for p in row.find_all("p")]
            ozet = " ".join(p for p in paragraphs if p).strip()
            detay_a = row.select_one("a[id='detay']")
            detay_id = None
            if detay_a and detay_a.get("href"):
                m = re.search(r"/DisiplinKararlariDetay/(\d+)", detay_a["href"])
                if m:
                    detay_id = int(m.group(1))
            if detay_id is not None:
                decisions.append(TbbDecisionSummary(
                    tarih=tarih,
                    esas_no=esas,
                    karar_no=karar,
                    ozet=ozet,
                    detay_id=detay_id,
                    document_id=f"tbb:{detay_id}"
                ))
        m_last = re.search(r"skipToLast\"><a href=\"[^\"]*page=(\d+)", content)
        last_page = int(m_last.group(1)) if m_last else None
        return decisions, last_page

    async def _fetch_page(self, arama: str, yil: Optional[int], page: int) -> Tuple[str, List[TbbDecisionSummary], Optional[int]]:
        query_params: Dict[str, Any] = {"arama": arama}
        if yil:
            query_params["yil"] = str(yil)
        if page > 1:
            query_params["page"] = str(page)
        response = await self.http_client.get(self.BASE_URL + self.SEARCH_PATH, params=query_params)
        response.raise_for_status()
        content = response.text
        decisions, last_page = self._parse_page(content)
        return content, decisions, last_page

    async def search_decisions(self, params: TbbSearchRequest) -> TbbSearchResult:
        """Search TBB Disiplin Kurulu decisions (with ç/ö/ü workaround)."""
        keywords = (params.keywords or "").strip()
        logger.info(f"TbbApiClient: Searching decisions with keywords={keywords!r}, yil={params.yil}, page={params.page}")

        if not keywords:
            return TbbSearchResult(
                decisions=[], total_results=0, page=params.page,
                pageSize=params.pageSize, query=None
            )

        tokens = keywords.split()
        workaround = self._has_breaking(keywords)

        site_query = " ".join(self._fold_broken(t) for t in tokens) if workaround else keywords
        if workaround and not tokens:
            site_query = keywords

        try:
            page_query = site_query
            content, first_decisions, last_page = await self._fetch_page(page_query, params.yil, 1)
            empty = "bulunamadı" in content.lower()

            # If the folded query yielded nothing, fall back to safe tokens only.
            if empty and workaround:
                safe_tokens = [t for t in tokens if not self._has_breaking(t)]
                if safe_tokens and " ".join(safe_tokens) != page_query:
                    page_query = " ".join(safe_tokens)
                    content, first_decisions, last_page = await self._fetch_page(page_query, params.yil, 1)
                    empty = "bulunamadı" in content.lower()

            if empty:
                logger.info(f"TbbApiClient: No decisions found for query {keywords!r} / {params.yil}")
                return TbbSearchResult(
                    decisions=[], total_results=0, page=params.page,
                    pageSize=params.pageSize, query=keywords
                )

            # Gather all pages (workaround needs the whole candidate set for local filtering).
            all_decisions: List[TbbDecisionSummary] = list(first_decisions)
            total_site_pages = last_page or 1
            page_limit = min(total_site_pages, self.MAX_SITE_PAGES)
            for p in range(2, page_limit + 1):
                try:
                    _, page_decisions, _ = await self._fetch_page(page_query, params.yil, p)
                    all_decisions.extend(page_decisions)
                except Exception as e:
                    logger.warning(f"TbbApiClient: page {p} failed: {e}")
                    break

            folded_tokens = [self._fold_full(t) for t in tokens]
            if workaround:
                matched = [
                    d for d in all_decisions
                    if all(t in self._fold_full(f"{d.ozet or ''} {d.esas_no or ''} {d.karar_no or ''} {d.tarih or ''}") for t in folded_tokens)
                ]
            else:
                matched = all_decisions

            total = len(matched)
            start = (params.page - 1) * params.pageSize
            page_items = matched[start:start + params.pageSize]

            logger.info(f"TbbApiClient: site pages={total_site_pages}, candidates={len(all_decisions)}, matched={total}")
            return TbbSearchResult(
                decisions=page_items,
                total_results=total,
                page=params.page,
                pageSize=params.pageSize,
                query=keywords
            )

        except httpx.RequestError as e:
            logger.error(f"TbbApiClient: HTTP request error during search: {e}")
        except Exception as e:
            logger.error(f"TbbApiClient: Unexpected error during search: {e}")
        return TbbSearchResult(
            decisions=[], total_results=0, page=params.page,
            pageSize=params.pageSize, query=keywords
        )

    async def get_document(
        self,
        detay_id: int,
        page_number: int = 1
    ) -> TbbDocumentMarkdown:
        """Retrieve a TBB Disiplin Kurulu decision full text as paginated Markdown."""
        logger.info(f"TbbApiClient: Getting decision document detay_id={detay_id}, page={page_number}")

        source_id = f"tbb:{detay_id}"
        source_url = f"{self.BASE_URL}{self.DETAIL_PATH}/{detay_id}"

        try:
            response = await self.http_client.get(source_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            boxes = soup.select("div.dskkutu")
            tarih = esas = karar = None
            for box in boxes:
                label = box.find("strong")
                if not label:
                    continue
                label_text = label.get_text(strip=True)
                value = box.get_text(" ", strip=True).replace(label_text, "", 1).strip()
                if label_text == "Tarih":
                    tarih = value
                elif label_text == "Esas":
                    esas = value
                elif label_text == "Karar":
                    karar = value

            ozet = ""
            blockquote = soup.find("blockquote")
            if blockquote:
                ozet = self._html_to_text(str(blockquote))

            detay_div = soup.find("div", class_="detay")
            full_text = ""
            if detay_div:
                raw = str(detay_div)
                full_text = self._html_to_text(raw)

            if not full_text.strip():
                return TbbDocumentMarkdown(
                    source_id=source_id, source_url=source_url,
                    tarih=tarih, esas_no=esas, karar_no=karar, ozet=ozet,
                    markdown_chunk=None, current_page=page_number,
                    total_pages=0, is_paginated=False,
                    error_message="Karar metni alınamadı."
                )

            content_length = len(full_text)
            total_pages = max(1, math.ceil(content_length / self.DOCUMENT_MARKDOWN_CHUNK_SIZE))
            current = max(1, min(page_number, total_pages))
            start = (current - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE
            chunk = full_text[start:start + self.DOCUMENT_MARKDOWN_CHUNK_SIZE]

            return TbbDocumentMarkdown(
                source_id=source_id, source_url=source_url,
                tarih=tarih, esas_no=esas, karar_no=karar, ozet=ozet,
                markdown_chunk=chunk, current_page=current,
                total_pages=total_pages, is_paginated=(total_pages > 1),
                error_message=None
            )

        except httpx.RequestError as e:
            logger.error(f"TbbApiClient: HTTP request error during document retrieval: {e}")
        except Exception as e:
            logger.error(f"TbbApiClient: Unexpected error during document retrieval: {e}")
        return TbbDocumentMarkdown(
            source_id=source_id, source_url=source_url,
            tarih=None, esas_no=None, karar_no=None, ozet=None,
            markdown_chunk=None, current_page=page_number,
            total_pages=0, is_paginated=False,
            error_message=f"Karar metni alınamadı: {e}"
        )

    async def close_client_session(self):
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("TbbApiClient: HTTP client session closed.")