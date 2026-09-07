# bedesten_mcp_module/query_parser.py
# -*- coding: utf-8 -*-

"""
Natural-language query parser for the unified Bedesten search.

Extracts structured search parameters (court types, chamber, date range,
case numbers) from free-form Turkish queries such as:

    "Yargıtay 1. Hukuk Dairesi mülkiyet hakkı 2023"
    "danıştay 3. daire vergi 2022-2024"
    "E.2023/1234 K.2024/567"
    "istinaf \"sözleşme ihlali\" asliye"

Everything not recognized as a structural token is left in the phrase used
for full-text search, so keyword search still works as before.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .enums import BIRIM_ADI_MAPPING

# All court types supported by the Bedesten API
ALLOWED_COURT_TYPES = ("YARGITAYKARARI", "DANISTAYKARAR", "YERELHUKUK", "ISTINAFHUKUK", "KYB")

DEFAULT_COURT_TYPES = ("YARGITAYKARARI", "DANISTAYKARAR")

# Court type rules (checked in order; first match wins)
COURT_TYPE_RULES = [
    (
        re.compile(
            r"\b(?:kanun yararına bozma|kanun yararina bozma|kyb)\b", re.I
        ),
        "KYB",
    ),
    (
        re.compile(
            r"\b(?:yargıtay|yargitay|yhgk|cgk|temyiz|hukuk genel kurulu|ceza genel kurulu)\b",
            re.I,
        ),
        "YARGITAYKARARI",
    ),
    (
        re.compile(
            r"\b(?:danıştay|danistay|iddk|vddk|vergi dava daireleri|idare dava daireleri|içtihatları birleştirme|icra dairesi)\b",
            re.I,
        ),
        "DANISTAYKARAR",
    ),
    (
        re.compile(r"\b(?:istinaf|bölge adliye|bolge adliye|bam)\b", re.I),
        "ISTINAFHUKUK",
    ),
    (
        re.compile(
            r"\b(?:asliye|sulh|aile mahkemesi|iş mahkemesi|is mahkemesi|tüketici mahkemesi|kadastro|icra mahkemesi|ticaret mahkemesi|yerel hukuk)\b",
            re.I,
        ),
        "YERELHUKUK",
    ),
]

# Chamber aliases (normalized lowercase -> abbreviated birimAdi value)
CHAMBER_ALIASES = {}
for _abbrev, _full in BIRIM_ADI_MAPPING.items():
    if _abbrev == "ALL" or _full is None:
        continue
    _key = re.sub(r"[^a-z0-9]", "", _full.lower())
    CHAMBER_ALIASES[_key] = _abbrev

# Chamber detection patterns (specific before generic)
CHAMBER_PATTERNS = [
    # Yargıtay civil/criminal chambers: "1. Hukuk Dairesi" / "2. Ceza Dairesi"
    (re.compile(r"\b(\d{1,2})\.?\s*hukuk\s+daire\w*\b", re.I), "H"),
    (re.compile(r"\b(\d{1,2})\.?\s*ceza\s+daire\w*\b", re.I), "C"),
    # Danıştay chambers: "3. Daire"
    (re.compile(r"\b(\d{1,2})\.?\s*daire\b(?!\s*(?:başkanlar|idari|dava))", re.I), "D"),
]

CHAMBER_FULL_PATTERNS = [
    (re.compile(r"\bhukuk\s+genel\s+kurulu\b", re.I), "HGK"),
    (re.compile(r"\bceza\s+genel\s+kurulu\b", re.I), "CGK"),
    (re.compile(r"\bbüyük\s+genel\s+kurulu\b", re.I), "BGK"),
    (re.compile(r"\bvergi\s+dava\s+daireleri\s+kurulu\b", re.I), "VDDK"),
    (re.compile(r"\bidare\s+dava\s+daireleri\s+kurulu\b", re.I), "IDDK"),
    (re.compile(r"\biçtihatları\s+birleştirme\s+kurulu\b", re.I), "IBK"),
    (re.compile(r"\bidari\s+işler\s+kurulu\b", re.I), "IIK"),
    (re.compile(r"\bdairesi\s+başkanlar\s+kurulu\b", re.I), "HBK"),
    (re.compile(r"\baskeri\s+yüksek\s+idare\s+mahkemesi\b", re.I), "AYIM"),
]

# Court type implied by each chamber
CHAMBER_COURT_HINT = {
    "H": "YARGITAYKARARI",
    "C": "YARGITAYKARARI",
    "D": "DANISTAYKARAR",
    "HGK": "YARGITAYKARARI",
    "CGK": "YARGITAYKARARI",
    "BGK": "YARGITAYKARARI",
    "HBK": "YARGITAYKARARI",
    "CBK": "YARGITAYKARARI",
    "VDDK": "DANISTAYKARAR",
    "IDDK": "DANISTAYKARAR",
    "IBK": "DANISTAYKARAR",
    "IIK": "DANISTAYKARAR",
    "DBK": "DANISTAYKARAR",
    "AYIM": "DANISTAYKARAR",
}

# Date patterns
_DATE_PATTERN = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_YEAR_RANGE_PATTERN = re.compile(r"\b((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})\b")
_YEAR_PATTERN = re.compile(r"\b((?:19|20)\d{2})\b")

# Case number patterns
_ESAS_LABEL_PATTERN = re.compile(
    r"\b(?:esas(?:\s*no)?|esasno)\s*[:.]?\s*(\d{4}/\d{1,6})\b", re.I
)
_KARAR_LABEL_PATTERN = re.compile(
    r"\b(?:karar(?:\s*no)?|kararno)\s*[:.]?\s*(\d{4}/\d{1,6})\b", re.I
)
_E_PATTERN = re.compile(r"\bE\.?\s*[:.]?\s*(\d{4}/\d{1,6})\b", re.I)
_K_PATTERN = re.compile(r"\bK\.?\s*[:.]?\s*(\d{4}/\d{1,6})\b", re.I)
_BARE_NUMBER_PATTERN = re.compile(r"\b(\d{4}/\d{1,6})\b")

# Phrases that carry meaning for court/chamber detection but should NOT go
# into full-text search
_STRUCTURAL_TOKENS = re.compile(
    r"\b(?:yargıtay|yargitay|danıştay|danistay|istinaf|bölge adliye|bolge adliye|bam|"
    r"temyiz|yerel|asliye|sulh|aile mahkemesi|iş mahkemesi|is mahkemesi|"
    r"tüketici mahkemesi|kadastro|icra mahkemesi|ticaret mahkemesi|yerel hukuk|"
    r"hukuk genel kurulu|ceza genel kurulu|büyük genel kurulu|buyuk genel kurulu|"
    r"vergi dava daireleri kurulu|idare dava daireleri kurulu|içtihatları birleştirme kurulu|"
    r"daireler başkanlar kurulu|başkanlar kurulu|askeri yüksek idare mahkemesi|"
    r"hukuk dairesi|ceza dairesi|dairesi|daire|kurulu|kurul|mahkemesi|mahkeme|"
    r"esas no|esasno|esas|karar no|kararno|kanun yararına bozma|kanun yararina bozma|kyb|"
    r"yıl|yılı|yili|tarihli|tarih|karar tarihi|"
    r"vddk|iddk|ibk|hgk|cgk|bgk|hbk|cbk|dbgk|iiak|ayim|yhgk)\b",
    re.I,
)


@dataclass
class ParsedQuery:
    """Structured search parameters extracted from a free-form query."""

    phrase: str = ""
    court_types: List[str] = field(default_factory=lambda: list(DEFAULT_COURT_TYPES))
    birim_adi: str = "ALL"
    karar_tarihi_start: str = ""
    karar_tarihi_end: str = ""
    esas_no: str = ""
    karar_no: str = ""
    detections: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "phrase": self.phrase,
            "court_types": self.court_types,
            "birim_adi": self.birim_adi,
            "karar_tarihi_start": self.karar_tarihi_start,
            "karar_tarihi_end": self.karar_tarihi_end,
            "esas_no": self.esas_no,
            "karar_no": self.karar_no,
            "detections": self.detections,
        }


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _detect_court_type(text: str, detections: List[str]) -> List[str]:
    found: List[str] = []
    for pattern, court_type in COURT_TYPE_RULES:
        m = pattern.search(text)
        if m and court_type not in found:
            found.append(court_type)
            detections.append(f"Mahkeme: {court_type} (eşleşen: '{m.group(0).strip()}')")
    return found


def _detect_chamber(text: str, detections: List[str]) -> tuple:
    """Detect a chamber mention, returning (abbrev, text_with_chamber_removed)."""
    for pattern, abbrev in CHAMBER_FULL_PATTERNS:
        m = pattern.search(text)
        if m:
            detections.append(f"Daire/Kurul: {abbrev} ({BIRIM_ADI_MAPPING.get(abbrev, abbrev)})")
            return abbrev, pattern.sub(" ", text, count=1)

    for pattern, prefix in CHAMBER_PATTERNS:
        m = pattern.search(text)
        if m:
            num = int(m.group(1))
            abbrev = f"{prefix}{num}"
            if abbrev in BIRIM_ADI_MAPPING:
                detections.append(f"Daire: {abbrev} ({BIRIM_ADI_MAPPING[abbrev]})")
                return abbrev, pattern.sub(" ", text, count=1)

    # Alias-based detection (exact normalized full names)
    for token in re.split(r"[\s,;()]+", text):
        key = _normalize(token)
        if key in CHAMBER_ALIASES:
            abbrev = CHAMBER_ALIASES[key]
            detections.append(f"Daire: {abbrev} ({BIRIM_ADI_MAPPING.get(abbrev, abbrev)})")
            return abbrev, re.sub(r"\b" + re.escape(token) + r"\b", " ", text, count=1)

    return "ALL", text


def _detect_dates(text: str, detections: List[str]) -> tuple:
    """Detect explicit dates and year ranges. Returns (start, end) ISO dates."""
    start = end = ""

    m = _DATE_PATTERN.search(text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        start = f"{y:04d}-{mo:02d}-{d:02d}T00:00:00.000Z"
        end = f"{y:04d}-{mo:02d}-{d:02d}T23:59:59.999Z"
        detections.append(f"Tarih: {y:04d}-{mo:02d}-{d:02d}")
        return start, end

    m = _YEAR_RANGE_PATTERN.search(text)
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        start = f"{y1:04d}-01-01T00:00:00.000Z"
        end = f"{y2:04d}-12-31T23:59:59.999Z"
        detections.append(f"Yıl aralığı: {y1} - {y2}")
        return start, end

    m = _YEAR_PATTERN.search(text)
    if m:
        y = int(m.group(1))
        start = f"{y:04d}-01-01T00:00:00.000Z"
        end = f"{y:04d}-12-31T23:59:59.999Z"
        detections.append(f"Yıl: {y}")
        return start, end

    return start, end


def _detect_case_numbers(text: str, detections: List[str]) -> tuple:
    """Detect esas/karar numbers. Returns (esas_no, karar_no, remaining_text)."""
    esas = karar = ""
    remaining = text

    m = _ESAS_LABEL_PATTERN.search(remaining)
    if m:
        esas = m.group(1)
        remaining = _ESAS_LABEL_PATTERN.sub(" ", remaining, count=1)
        detections.append(f"Esas No: {esas}")
    else:
        m = _E_PATTERN.search(remaining)
        if m:
            esas = m.group(1)
            remaining = _E_PATTERN.sub(" ", remaining, count=1)
            detections.append(f"Esas No: {esas}")

    m = _KARAR_LABEL_PATTERN.search(remaining)
    if m:
        karar = m.group(1)
        remaining = _KARAR_LABEL_PATTERN.sub(" ", remaining, count=1)
        detections.append(f"Karar No: {karar}")
    else:
        m = _K_PATTERN.search(remaining)
        if m:
            karar = m.group(1)
            remaining = _K_PATTERN.sub(" ", remaining, count=1)
            detections.append(f"Karar No: {karar}")

    # Bare number without any label: assume it is a case number (esas).
    if not esas and not karar:
        m = _BARE_NUMBER_PATTERN.search(remaining)
        if m:
            esas = m.group(1)
            remaining = _BARE_NUMBER_PATTERN.sub(" ", remaining, count=1)
            detections.append(f"Esas No (etiketli değil): {esas}")

    return esas, karar, remaining


def _clean_phrase(text: str, remove_years: bool = False) -> str:
    """Remove structural tokens while preserving quoted phrases and operators."""
    # Protect quoted phrases with placeholders so their content AND position
    # survive cleaning (previously they were re-appended at the end, which
    # broke queries like  "marka" AND "finansal kiralama"  ->  AND "marka" ...)
    placeholders: dict = {}

    def _store(match) -> str:
        key = f"__QUOTED_{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key

    protected = re.sub(r'"[^"]+"', _store, text)

    cleaned = _STRUCTURAL_TOKENS.sub(" ", protected)

    # Years become date filters; drop them from the full-text phrase when a
    # date range was already detected (also removes the dashes that joined
    # two years, e.g. "2021-2023").
    if remove_years:
        cleaned = re.sub(r"\b(?:19|20)\d{2}\b", " ", cleaned)
        cleaned = re.sub(r"\s+[-–—]\s+", " ", cleaned)

    # Remove leftover standalone letters (E/K markers) and stray punctuation
    cleaned = re.sub(r"\b[EeKk]\b", " ", cleaned)
    cleaned = re.sub(r"[^\w\s+\-&|]", " ", cleaned, flags=re.UNICODE)

    # Restore quoted phrases in their original position
    for key, value in placeholders.items():
        cleaned = cleaned.replace(key, value)

    parts = [p.strip() for p in re.split(r"\s+", cleaned) if p.strip()]
    # Keep boolean operators and + / - prefixes attached to terms
    return " ".join(parts)


def _looks_like_boolean_query(text: str) -> bool:
    """True when the query already carries explicit search operators."""
    if re.search(r"\b(?:AND|OR|NOT)\b", text, re.I):
        return True
    if re.search(r"(^|\s)[+-][\w\"\']", text):
        return True
    if '"' in text:
        return True
    return False


def _apply_and_semantics(phrase: str) -> str:
    """
    Convert a loose multi-word phrase into an implicit AND query by prefixing
    each term with '+'. Bedesten otherwise matches the words loosely (OR-like),
    which floods the result set with irrelevant decisions for long queries.
    Quoted phrases, boolean operators, case numbers and + / - prefixes are
    left untouched (callers must check _looks_like_boolean_query first).
    """
    terms = [t.strip() for t in phrase.split() if t.strip()]
    if len(terms) < 3:
        return phrase
    if any(t.startswith(("+", "-")) for t in terms):
        return phrase
    return " ".join(f"+{t}" for t in terms)


def parse_search_query(query: str) -> ParsedQuery:
    """
    Parse a free-form Turkish legal query into structured Bedesten parameters.

    Returns a ParsedQuery with:
      - phrase: cleaned full-text search term
      - court_types: detected court types (defaults if none found)
      - birim_adi: detected chamber (abbrev) or "ALL"
      - karar_tarihi_start/end: ISO-8601 date range
      - esas_no / karar_no: detected case numbers
      - detections: human-readable notes about what was extracted
    """
    if not query or not query.strip():
        return ParsedQuery(phrase="", detections=["Boş sorgu: filtre parametrelerini kullanın."])

    text = query.strip()

    parsed = ParsedQuery()
    detections = parsed.detections

    # 1) Case numbers first (they contain years that would confuse date detection)
    esas_no, karar_no, rest = _detect_case_numbers(text, detections)
    parsed.esas_no = esas_no
    parsed.karar_no = karar_no

    # 2) Court types
    courts = _detect_court_type(rest, detections)
    if courts:
        parsed.court_types = courts

    # 3) Chamber (also removes the chamber mention from the query text)
    parsed.birim_adi, rest = _detect_chamber(rest, detections)

    # 4) Chamber implies a court type when no explicit court was mentioned
    if not courts and parsed.birim_adi != "ALL":
        hinted = CHAMBER_COURT_HINT.get(parsed.birim_adi[:1])
        if hinted is None and parsed.birim_adi in CHAMBER_COURT_HINT:
            hinted = CHAMBER_COURT_HINT[parsed.birim_adi]
        if hinted:
            parsed.court_types = [hinted]
            detections.append(f"Mahkeme (daireden çıkarım): {hinted}")

    # 5) Dates (years already removed from rest via case numbers where possible)
    parsed.karar_tarihi_start, parsed.karar_tarihi_end = _detect_dates(rest, detections)

    # 6) Cleaned full-text phrase (years dropped only when used as date filter)
    remove_years = bool(parsed.karar_tarihi_start or parsed.karar_tarihi_end)
    phrase = _clean_phrase(rest, remove_years=remove_years)
    if esas_no:
        phrase = (phrase + f' "{esas_no}"').strip()
    if karar_no:
        phrase = (phrase + f' "{karar_no}"').strip()

    # 7) Implicit AND semantics for loose multi-word queries: Bedesten matches
    #    bare multi-word phrases loosely (OR-like), flooding results. Prefix
    #    each term with '+' unless the user already used explicit operators.
    if phrase and not esas_no and not karar_no and not _looks_like_boolean_query(rest):
        and_phrase = _apply_and_semantics(phrase)
        if and_phrase != phrase:
            phrase = and_phrase
            detections.append(
                "Çok kelimeli sorgu: AND semantiği uygulandı (her terim '+' ile zorunlu yapıldı)"
            )
    parsed.phrase = phrase

    if not detections:
        detections.append("Yapısal filtre bulunamadı; tüm sorgu anahtar kelime olarak kullanıldı.")

    return parsed
