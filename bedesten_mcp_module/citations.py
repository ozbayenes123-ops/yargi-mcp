# -*- coding: utf-8 -*-
"""Extract precedent citations from Turkish court decision texts.

Turkish decisions cite prior case-law with case numbers such as
"E. 2019/1500 K. 2019/6500", "2019/1500 E., 2019/6500 K." or
"2020/1234 sayılı kararı". This module finds those references, attaches a
short context snippet, and excludes the decision's own case numbers.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# "E. 2019/1500 K. 2019/6500", "E.2019/1500 K.2019/6500", "E. 2019/1500 sayılı K. 2019/6500"
_PATTERN_PAIR_EK = re.compile(
    r"\bE\.?\s*(\d{4})/(\d{1,6})\s*(?:sayılı\s+)?\s*K\.?\s*(\d{4})/(\d{1,6})\b",
    re.IGNORECASE,
)
# "2019/1500 E., 2019/6500 K." (number-first order)
_PATTERN_PAIR_KE = re.compile(
    r"\b(\d{4})/(\d{1,6})\s*E\.?\s*[.,;\s]+\s*(\d{4})/(\d{1,6})\s*K\.?\b",
    re.IGNORECASE,
)
# Single references: "2020/1234 sayılı kararı", "2019/6500 sayılı ilamına",
# "2021/100 sayılı kararımız". The negative lookahead skips header forms
# like "2024/1234 Karar No: 2025/5678".
_PATTERN_SINGLE = re.compile(
    r"\b(\d{4})/(\d{1,6})(?!\s*(?:esas|karar)\s*no\b)\s*(?:sayılı\s+)?"
    r"(?:karar(?:ımız|ımıza|ımızla|ının|ına|ıyla|ı)?|ilam(?:ımız|ımıza|ımızla|ının|ına|ıyla|ı)?)\b",
    re.IGNORECASE,
)


def _clean_window(text: str, start: int, end: int) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - 80): end + 120]).strip()


def extract_citations(
    text: str,
    own_numbers: Optional[Iterable[Tuple[str, str]]] = None,
    maks_atif: int = 20,
) -> List[Dict[str, Any]]:
    """
    Extract cited case numbers from a decision text.

    Args:
        text: Full decision text (plain/markdown).
        own_numbers: The decision's own (esas_no, karar_no) pairs to exclude.
        maks_atif: Maximum number of distinct citations to return.

    Returns:
        List of dicts: {"esas_no", "karar_no", "atif_turu", "baglam"}.
        ``atif_turu`` is "esas_karar" for pair citations, "karar_no" for
        single-number references (stored in ``karar_no``).
    """
    own = {(str(e), str(k)) for e, k in (own_numbers or []) if e or k}
    # A decision's own numbers also exclude single-number references to them
    own |= {("", str(k)) for _, k in own if k} | {(str(e), "") for e, _ in own if e}
    seen: set = set()
    seen_numbers: set = set()
    citations: List[Dict[str, Any]] = []

    def _add(esas: str, karar: str, start: int, end: int, atif_turu: str) -> bool:
        if (esas, karar) in own or (esas, karar) in seen:
            return False
        # A single-number reference that already appeared inside a pair is a
        # duplicate ("2018/900 sayılı kararı" after "E. 2018/400 K. 2018/900").
        if atif_turu == "karar_no" and karar in seen_numbers:
            return False
        seen.add((esas, karar))
        if esas:
            seen_numbers.add(esas)
        if karar:
            seen_numbers.add(karar)
        citations.append({
            "esas_no": esas,
            "karar_no": karar,
            "atif_turu": atif_turu,
            "baglam": _clean_window(text, start, end),
        })
        return len(citations) >= maks_atif

    for m in _PATTERN_PAIR_EK.finditer(text):
        if _add(f"{m.group(1)}/{m.group(2)}", f"{m.group(3)}/{m.group(4)}", m.start(), m.end(), "esas_karar"):
            break
    if len(citations) < maks_atif:
        for m in _PATTERN_PAIR_KE.finditer(text):
            if _add(f"{m.group(1)}/{m.group(2)}", f"{m.group(3)}/{m.group(4)}", m.start(), m.end(), "esas_karar"):
                break
    if len(citations) < maks_atif:
        for m in _PATTERN_SINGLE.finditer(text):
            if _add("", f"{m.group(1)}/{m.group(2)}", m.start(), m.end(), "karar_no"):
                break

    return citations[:maks_atif]
