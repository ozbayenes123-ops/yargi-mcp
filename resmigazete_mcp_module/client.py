# resmigazete_mcp_module/client.py

import asyncio
import io
import logging
import re
import ssl
import unicodedata
from datetime import date, timedelta
from typing import List, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .models import (
    FihristItem,
    FihristResult,
    FihristSection,
    GazetteArticleMarkdown,
    GazetteSearchMatch,
    GazetteSearchResult,
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


class ResmiGazeteApiClient:
    """
    Client for the Turkish Official Gazette (Resmî Gazete, resmigazete.gov.tr).

    The site serves plain server-rendered HTML: per-issue fihrist pages at
    /eskiler/YYYY/MM/YYYYMMDD[M1..M5].htm, article pages at /eskiler/YYYY/MM/YYYYMMDD-N.htm
    (or .pdf). This client fetches fihrist pages, parses sections/articles, reads
    article HTML and extracts text, and can search fihrist titles over a date range.
    """

    BASE_URL = "https://www.resmigazete.gov.tr"
    MAX_RANGE_DAYS = 730
    CONCURRENCY = 5

    def __init__(self, request_timeout: float = 45.0):
        # resmigazete.gov.tr serves a certificate chain that fails verification
        # on some clients; follow the KİK module pattern with a legacy SSL context.
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ssl_context.options |= ssl.OP_LEGACY_SERVER_CONNECT
        ssl_context.set_ciphers("ALL:!aNULL:!eNULL:!EXPORT:!DES:!RC4:!MD5:!PSK:!SRP:!CAMELLIA")
        self.http_client = httpx.AsyncClient(
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            timeout=request_timeout,
            verify=ssl_context,
            follow_redirects=True,
        )

    @staticmethod
    def build_fihrist_url(issue_date: str, mukerrer_index: int = 0) -> str:
        y, m, d = issue_date.split("-")
        stem = f"{y}{m}{d}"
        suffix = f"M{mukerrer_index}" if mukerrer_index > 0 else ""
        return f"https://www.resmigazete.gov.tr/eskiler/{y}/{m}/{stem}{suffix}.htm"

    @staticmethod
    def _clean_text(s: str) -> str:
        return re.sub(r"\s+", " ", s or "").strip()

    @staticmethod
    def _normalize(s: str) -> str:
        lowered = (s or "").lower()
        return (
            unicodedata.normalize("NFKD", lowered)
            .encode("ascii", "ignore")
            .decode("ascii")
            .replace("\u0131", "i")
            .replace("\u011f", "g")
            .replace("\u015f", "s")
        )

    def _parse_fihrist(self, html: str, issue_date: str, source_url: str) -> FihristResult:
        soup = BeautifulSoup(html, "html.parser")
        sections: List[FihristSection] = []
        current: Optional[FihristSection] = None

        for el in soup.find_all(["b", "strong", "a"]):
            if el.name in ("b", "strong"):
                text = self._clean_text(el.get_text())
                if (
                    text
                    and (text.endswith("BÖLÜM") or text.endswith("BÖLÜMÜ") or "İLÂN BÖLÜMÜ" in text or "İLAN BÖLÜMÜ" in text)
                    and len(text) < 120
                ):
                    current = FihristSection(name=text, items=[])
                    sections.append(current)
                continue

            href = el.get("href", "")
            if not href or not re.search(r"\.(htm|html|pdf)(\?|$)", href, re.I):
                continue
            if re.search(r"fihrist|index|default\.aspx|main\.aspx|main", href, re.I):
                continue
            full = urljoin(source_url, href)
            match = re.search(r"eskiler/\d{4}/\d{2}/(\d{8})(M\d+)?-\d+\.(htm|html|pdf)", full, re.I)
            if not match:
                continue
            is_pdf = bool(re.search(r"\.pdf(\?|$)", full, re.I))
            title = self._clean_text(el.get_text()) or self._clean_text(el.find_parent(["tr", "p", "div"]).get_text() if el.find_parent(["tr", "p", "div"]) else "")
            if not title:
                continue

            if current is None:
                current = FihristSection(name="Genel", items=[])
                sections.append(current)

            base = re.sub(r"\.pdf(\?|$)", ".htm", full)
            existing = next((it for it in current.items if it.url == base or it.pdf_url == full), None)
            if is_pdf:
                if existing:
                    existing.pdf_url = full
                else:
                    current.items.append(FihristItem(title=title, url=full, pdf_url=full, is_pdf=True))
            else:
                if existing and not existing.title:
                    existing.title = title
                elif not existing:
                    current.items.append(FihristItem(title=title, url=full, pdf_url=None, is_pdf=False))

        return FihristResult(date=issue_date, source_url=source_url, sections=sections)

    async def _fetch_fihrist(self, issue_date: str, mukerrer_index: int = 0) -> Optional[FihristResult]:
        url = self.build_fihrist_url(issue_date, mukerrer_index)
        try:
            response = await self.http_client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            response.encoding = "windows-1254"
            return self._parse_fihrist(response.text, issue_date, url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            logger.warning("ResmiGazeteApiClient: HTTP %s on %s", e.response.status_code, url)
            return None
        except Exception as e:
            logger.warning("ResmiGazeteApiClient: fihrist fetch error for %s: %s", url, e)
            return None

    async def get_fihrist(self, issue_date: str, include_mukerrer: bool = True) -> FihristResult:
        """Fetch and parse the fihrist for a given date (plus mükerrer issues)."""
        main = await self._fetch_fihrist(issue_date, 0)
        if main is None:
            return FihristResult(
                date=issue_date,
                error_message=f"No gazette found for {issue_date}.",
            )

        if include_mukerrer:
            mukerrer: List[FihristResult] = []
            for i in range(1, 6):
                mk = await self._fetch_fihrist(issue_date, i)
                if mk is None:
                    break
                mukerrer.append(mk)
            if mukerrer:
                main.mukerrer = mukerrer

        return main

    async def get_article_markdown(self, article_url: str) -> GazetteArticleMarkdown:
        """Fetch a single article page (.htm or .pdf) and extract its text."""
        logger.info("ResmiGazeteApiClient: fetching article %s", article_url)
        try:
            response = await self.http_client.get(article_url)
            response.raise_for_status()
            if article_url.lower().endswith(".pdf"):
                return await asyncio.to_thread(self._extract_pdf_text, response.content, article_url)
            response.encoding = "windows-1254"
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "head"]):
                tag.decompose()
            title = (
                soup.title.get_text(strip=True)
                if soup.title
                else (soup.h1.get_text(strip=True) if soup.h1 else (soup.b.get_text(strip=True) if soup.b else ""))
            )
            text = soup.get_text(separator="\n")
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n", text).strip()
            return GazetteArticleMarkdown(url=article_url, title=title[:500], markdown_content=text)
        except Exception as e:
            logger.error("ResmiGazeteApiClient: article fetch error for %s: %s", article_url, e)
            return GazetteArticleMarkdown(
                url=article_url,
                error_message=f"Error retrieving article: {str(e)}",
            )

    @staticmethod
    def _extract_pdf_text(pdf_bytes: bytes, pdf_url: str) -> GazetteArticleMarkdown:
        """Extract text from a PDF article using pypdf."""
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(pdf_bytes))
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
            text = "\n".join(pages)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n", text).strip()
            return GazetteArticleMarkdown(
                url=pdf_url,
                title=text.splitlines()[0][:500] if text.splitlines() else "",
                markdown_content=text,
            )
        except Exception as e:
            logger.error("ResmiGazeteApiClient: PDF text extraction error for %s: %s", pdf_url, e)
            return GazetteArticleMarkdown(
                url=pdf_url,
                error_message=f"Error extracting PDF text: {str(e)}",
            )

    async def search_fihrist_titles(
        self, query: str, from_date: str, to_date: str, max_results: int = 50
    ) -> GazetteSearchResult:
        """Search fihrist titles across a date range for a keyword."""
        if not query.strip():
            return GazetteSearchResult(
                error_message="query must not be empty.",
            )
        try:
            start = date.fromisoformat(from_date)
            end = date.fromisoformat(to_date)
        except ValueError:
            return GazetteSearchResult(
                error_message="Invalid date format; expected YYYY-MM-DD.",
            )
        if start > end:
            return GazetteSearchResult(
                error_message="from_date must be <= to_date.",
            )
        diff_days = (end - start).days
        if diff_days > self.MAX_RANGE_DAYS:
            return GazetteSearchResult(
                error_message=f"Date range too wide; limit is {self.MAX_RANGE_DAYS} days.",
            )

        normalized_query = self._normalize(query)
        matches: List[GazetteSearchMatch] = []
        truncated = False

        cursor = start
        days_list: List[str] = []
        while cursor <= end:
            if cursor.weekday() != 6:  # Resmî Gazete is not published on Sundays
                days_list.append(cursor.isoformat())
            cursor += timedelta(days=1)

        for i in range(0, len(days_list), self.CONCURRENCY):
            batch = days_list[i : i + self.CONCURRENCY]
            results = await asyncio.gather(
                *[self.get_fihrist(d, include_mukerrer=True) for d in batch],
                return_exceptions=True,
            )
            for res in results:
                if isinstance(res, Exception) or res.error_message:
                    continue
                all_issues = [res] + list(res.mukerrer)
                for f in all_issues:
                    for section in f.sections:
                        for item in section.items:
                            haystack = self._normalize(item.title)
                            idx = haystack.find(normalized_query)
                            if idx == -1:
                                continue
                            snippet_start = max(0, idx - 40)
                            snippet_end = min(len(item.title), idx + len(query) + 80)
                            matches.append(
                                GazetteSearchMatch(
                                    date=f.date,
                                    section=section.name,
                                    title=item.title,
                                    url=item.url,
                                    pdf_url=item.pdf_url,
                                    snippet=item.title[snippet_start:snippet_end],
                                )
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                return GazetteSearchResult(
                                    matches=matches[:max_results],
                                    scanned_days=i + len(batch),
                                    truncated=True,
                                )

        return GazetteSearchResult(
            matches=matches,
            scanned_days=len(days_list),
            truncated=truncated,
        )

    async def close_client_session(self):
        """Close the HTTP client session."""
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("ResmiGazeteApiClient: HTTP client session closed.")
