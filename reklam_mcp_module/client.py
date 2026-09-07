# reklam_mcp_module/client.py

import io
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from pypdf import PdfReader

from .models import (
    ReklamBultenSummary,
    ReklamBultenListe,
    ReklamKararEslestirme,
    ReklamBultenIciAramaSonucu,
    ReklamBultenMarkdown
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

LIST_URL = "https://ticaret.gov.tr/tuketici/ticari-reklamlar/reklam-kurulu-kararlari"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
BULTEN_TTL_SECONDS = 30 * 24 * 3600

YEAR_HEADER_RE = re.compile(
    r"(\d{4})\s*YILI REKLAM KURULU BASIN BÜLTENLERİ", re.IGNORECASE)
ENTRY_RE = re.compile(
    r"(?:(\d{1,2}\.\d{1,2}\.\d{4})|(\d{1,2})\s+([A-Za-zğüşıöçâîû]+)\s+(\d{4}))\s*tarihli\s*(\d+)\s*Sayılı Reklam Kurulu Toplantısı Basın Bültenine\s*buradan\s*<a\s+href=\"([^\"]+)\"",
    re.IGNORECASE | re.DOTALL)

MONTHS_TR = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12
}

DOCX_URL_HINT = (".docx", ".doc")


def _fold(text: str) -> str:
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


class ReklamApiClient:
    """
    API client for Reklam Kurulu (Ticaret Bakanlığı) basın bültenleri:
      - Lists bulletins (toplantı no, tarih, yıl) from the official page
      - Downloads a bulletin (PDF or DOCX), extracts text (pypdf / markitdown)
      - Splits the bulletin into decisions on "Dosya No" boundaries
      - Searches inside a bulletin by keywords (diacritic-insensitive)
    """

    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000

    def __init__(self, request_timeout: float = 90.0):
        self.http_client = httpx.AsyncClient(
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            timeout=request_timeout,
            follow_redirects=True
        )
        self._liste: Optional[List[ReklamBultenSummary]] = None
        self._liste_fetched_at: Optional[float] = None

    # ---------- listing ----------

    async def list_bultenler(self, yil: Optional[int] = None) -> ReklamBultenListe:
        """List Reklam Kurulu basın bültenleri (optional year filter)."""
        logger.info(f"ReklamApiClient: listing bültenler yil={yil}")
        try:
            if self._liste is None or (datetime.now().timestamp() - (self._liste_fetched_at or 0)) > 3600:
                response = await self.http_client.get(LIST_URL)
                response.raise_for_status()
                content = response.text.replace("\u200b", "")
                self._liste = self._parse_entries(content)
                self._liste_fetched_at = datetime.now().timestamp()
                logger.info(f"ReklamApiClient: parsed {len(self._liste)} bültenler")

            result = self._liste
            if yil is not None:
                result = [b for b in result if b.yil == yil]
            return ReklamBultenListe(bultenler=result, yil=yil, total=len(result))

        except Exception as e:
            logger.error(f"ReklamApiClient: listing failed: {e}")
            return ReklamBultenListe(
                bultenler=[], yil=yil, total=0,
                error_message=f"Bülten listesi alınamadı: {e}"
            )

    @classmethod
    def _parse_entries(cls, content: str) -> List[ReklamBultenSummary]:
        """Parse year sections and bulletin entries from the page HTML."""
        parts = YEAR_HEADER_RE.split(content)
        # parts: [pre, year1, chunk1, year2, chunk2, ...]
        entries: List[ReklamBultenSummary] = []
        for i in range(1, len(parts) - 1, 2):
            year = int(parts[i])
            chunk = parts[i + 1]
            for m in ENTRY_RE.finditer(chunk):
                dot_date, day, month_name, year4, no, href = m.groups()
                if dot_date:
                    try:
                        tarih = datetime.strptime(dot_date, "%d.%m.%Y").strftime("%Y-%m-%d")
                    except ValueError:
                        tarih = dot_date
                    eff_year = year
                else:
                    month = MONTHS_TR.get((month_name or "").lower())
                    if month is None:
                        month = 1
                    tarih = f"{year4}-{month:02d}-{int(day):02d}"
                    eff_year = int(year4)
                entries.append(ReklamBultenSummary(
                    toplanti_no=int(no),
                    toplanti_tarihi=tarih,
                    yil=eff_year,
                    pdf_url=href.strip(),
                    document_id=f"reklam:{no}"
                ))
        entries.sort(key=lambda b: b.toplanti_no, reverse=True)
        return entries

    # ---------- bulletin text ----------

    def _bulten_text_path(self, no: int) -> Path:
        return CACHE_DIR / f"reklam_{no}.txt"

    def _read_bulten_text(self, no: int) -> Optional[str]:
        try:
            path = self._bulten_text_path(no)
            if not path.exists():
                return None
            if (datetime.now().timestamp() - path.stat().st_mtime) > BULTEN_TTL_SECONDS:
                return None
            return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"ReklamApiClient: cache read failed bülten {no}: {e}")
            return None

    def _write_bulten_text(self, no: int, text: str):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._bulten_text_path(no).write_text(text, encoding="utf-8")
        except Exception as e:
            logger.warning(f"ReklamApiClient: cache write failed bülten {no}: {e}")

    async def _resolve_bulten(self, no: int) -> Tuple[str, str]:
        """Return (text, url) for a bulletin, resolving the list entry for its URL."""
        liste = await self.list_bultenler()
        entry = next((b for b in liste.bultenler if b.toplanti_no == no), None)
        if entry is None:
            raise ValueError(f"{no} numaralı bülten bulunamadı (listede 199-370 arası kayıtlar var).")
        url = entry.pdf_url or ""
        return url, url

    @staticmethod
    def _extract_text(raw: bytes, url_hint: str) -> str:
        """Extract plain text from a bulletin PDF or DOCX."""
        if raw.startswith(b"%PDF"):
            reader = PdfReader(io.BytesIO(raw))
            return "\n\n".join(pg.extract_text() or "" for pg in reader.pages)
        if raw[:2] == b"PK" or url_hint.endswith(DOCX_URL_HINT):
            from markitdown import MarkItDown
            md_converter = MarkItDown(enable_plugins=False)
            md_result = md_converter.convert_stream(io.BytesIO(raw))
            return md_result.text_content or ""
        raise ValueError("Desteklenmeyen belge formatı (PDF/DOCX değil).")

    async def _get_bulten_text(self, no: int) -> Tuple[str, str]:
        """Bulletin full text (disk-cached), returns (text, url)."""
        url, _ = await self._resolve_bulten(no)
        cached = self._read_bulten_text(no)
        if cached is not None:
            return cached, url
        fetch_url = url.replace(" ", "%20")
        response = await self.http_client.get(fetch_url)
        response.raise_for_status()
        if response.status_code != 200 or not response.content:
            raise ValueError(f"Bülten indirilemedi: HTTP {response.status_code}")
        text = self._extract_text(response.content, url)
        text = re.sub(r"[ \t\u00a0]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        self._write_bulten_text(no, text)
        return text, url

    @staticmethod
    def _split_decisions(text: str) -> List[Dict[str, str]]:
        """Split bulletin text into decisions on 'Dosya No' boundaries."""
        raw_parts = re.split(r"(?=Dosya\s*No\s*[:：])", text)
        decisions: List[Dict[str, str]] = []
        for part in raw_parts:
            part = part.strip()
            if not part or not re.search(r"Dosya\s*No\s*[:：]", part):
                continue
            decisions.append({"text": part})
        return decisions

    @staticmethod
    def _decision_meta(part: str) -> Dict[str, Optional[str]]:
        lines = [l.strip() for l in part.splitlines() if l.strip()]
        dosya = re.search(r"Dosya\s*No\s*[:：]\s*([^\n]+)", part)
        sikayet = re.search(r"Şikayet\s*Edilen\s*[:：]\s*([^\n]+)", part)
        kategori = re.search(r"İlgili\s*Kategori\s*[:：]\s*([^\n]+)", part)
        karar_no = None
        m = re.search(r"^\s*(\d+)\)", part)
        if m:
            karar_no = m.group(1)
        return {
            "dosya_no": dosya.group(1).strip() if dosya else None,
            "sikayet_edilen": sikayet.group(1).strip() if sikayet else None,
            "kategori": kategori.group(1).strip() if kategori else None,
            "karar_no": karar_no
        }

    # ---------- in-bulletin search ----------

    async def search_bulten_ici(
        self,
        toplanti_no: int,
        keywords: str,
        max_results: int = 10
    ) -> ReklamBultenIciAramaSonucu:
        """Search keywords inside one bulletin; returns matching decisions with excerpts."""
        logger.info(f"ReklamApiClient: bülten {toplanti_no} içinde '{keywords}' aranıyor")
        try:
            text, _ = await self._get_bulten_text(toplanti_no)
        except Exception as e:
            logger.error(f"ReklamApiClient: bülten {toplanti_no} okunamadı: {e}")
            return ReklamBultenIciAramaSonucu(
                toplanti_no=toplanti_no, keywords=keywords, total=0,
                kararlar=[], error_message=str(e)
            )

        tokens = [_fold(t) for t in keywords.split() if t.strip()]
        if not tokens:
            return ReklamBultenIciAramaSonucu(
                toplanti_no=toplanti_no, keywords=keywords, total=0,
                kararlar=[], error_message="Anahtar kelime boş."
            )

        matches: List[ReklamKararEslestirme] = []
        for decision in self._split_decisions(text):
            part = decision["text"]
            folded = _fold(part)
            hits = sum(folded.count(t) for t in tokens)
            if hits == 0:
                continue
            meta = self._decision_meta(part)
            first_hit = min(folded.find(t) for t in tokens if t in folded)
            excerpt = re.sub(r"\s+", " ", part)[max(0, first_hit - 200):first_hit + 200]
            matches.append(ReklamKararEslestirme(
                karar_no=meta["karar_no"],
                dosya_no=meta["dosya_no"],
                sikayet_edilen=meta["sikayet_edilen"],
                kategori=meta["kategori"],
                alinti=excerpt,
                alaka_skoru=hits
            ))

        matches.sort(key=lambda m: m.alaka_skoru, reverse=True)
        matches = matches[:max(1, min(max_results, 25))]
        return ReklamBultenIciAramaSonucu(
            toplanti_no=toplanti_no, keywords=keywords,
            total=len(matches), kararlar=matches
        )

    # ---------- full bulletin ----------

    async def get_bulten_markdown(
        self,
        toplanti_no: int,
        page_number: int = 1
    ) -> ReklamBultenMarkdown:
        """Return the full bulletin text as paginated Markdown."""
        logger.info(f"ReklamApiClient: bülten {toplanti_no} markdown, sayfa {page_number}")
        source_id = f"reklam:{toplanti_no}"
        try:
            text, url = await self._get_bulten_text(toplanti_no)
        except Exception as e:
            logger.error(f"ReklamApiClient: bülten {toplanti_no} okunamadı: {e}")
            return ReklamBultenMarkdown(
                source_id=source_id, source_url=None, toplanti_no=toplanti_no,
                markdown_chunk=None, current_page=page_number,
                total_pages=0, is_paginated=False,
                error_message=str(e)
            )

        tarih_m = re.search(r"Toplantı\s*Tarihi\s*[:：]\s*([^\n]+)", text)
        sayi_m = re.search(r"Toplantı\s*Sayısı\s*[:：]\s*([^\n]+)", text)
        tarih = tarih_m.group(1).strip() if tarih_m else None
        sayi = sayi_m.group(1).strip() if sayi_m else None

        karar_sayisi = len(self._split_decisions(text))
        total_pages = max(1, math.ceil(len(text) / self.DOCUMENT_MARKDOWN_CHUNK_SIZE))
        current = max(1, min(page_number, total_pages))
        start = (current - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE
        chunk = text[start:start + self.DOCUMENT_MARKDOWN_CHUNK_SIZE]

        return ReklamBultenMarkdown(
            source_id=source_id, source_url=url,
            toplanti_no=toplanti_no, toplanti_tarihi=tarih,
            karar_sayisi=karar_sayisi,
            markdown_chunk=chunk, current_page=current,
            total_pages=total_pages, is_paginated=(total_pages > 1),
            error_message=None
        )

    async def close_client_session(self):
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("ReklamApiClient: HTTP client session closed.")