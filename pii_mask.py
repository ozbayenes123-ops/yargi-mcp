# -*- coding: utf-8 -*-
"""
Kişisel veri (PII) maskeleyici — dilekçe/dava dosyalarını yapay zekâya
göndermeden önce gerçek kişisel verileri takma etiketlerle değiştirir.

Amaç: hiçbir gerçek kimlik/adres/telefon/IBAN gibi veri dışarı çıkmasın;
buna karşılık metnin hukuki anlamı ve taraflar arası ilişkiler korunsun.
Maskeler tutarlıdır: aynı gerçek değer her yerde aynı etiketi alır.
Geri eşleme haritası (manifest) istenirse yerel dosyada saklanır,
AI'a gönderilmez.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

_PATTERNS: List[Tuple[str, str, str]] = [
    # (etiket kökü, regex, açıklama)
    ("TCKN", r"[1-9]\d{10}", "T.C. kimlik no"),
    ("VERGI", r"\d{10}", "vergi/VKN (10 hane)"),
    ("IBAN", r"TR\d{2}[0-9A-Z]{22}", "IBAN"),
    ("TELEFON", r"(?<!\d)(?:\+?90\s?)?(?:0?5\d{2}|0?2\d{2}|0?3\d{2}|0?4\d{2})[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)", "telefon"),
    ("EPASTA", r"[\w.+-]+@[\w-]+\.[\w.-]+", "e-posta"),
    ("ESASNO", r"[1-9]\d{2,4}/\d{2,6}", "esas/karar no"),
    ("TARIH", r"\d{2}[./-]\d{2}[./-]\d{4}", "tarih (gg.aa.yyyy)"),
    ("SAAT", r"\d{1,2}[.:]\d{2}", "saat"),
    ("MAHKEME_NO", r"\d{2,3}\s*(?:\.\s*)?(?:Asliye|Sulh|Ağır Ceza|İş|Ticaret|İdare|Vergi)", "mahkeme sıra"),
]

# Kişi unvanlı isim öbekleri: "Ahmet Yılmaz", "Mehmet Ali Şahin" gibi
_NAME_RE = re.compile(
    r"(?:Av\.|Av\.|Sayın|Bay|Bayan)?\s?"
    r"([A-ZÇĞİÖŞÜ][a-zçğıöşü]+(?:\s+[A-ZÇĞİÖŞÜ][a-zçğıöşü]+){1,3})"
)

_ROLE_HINTS = [
    ("DAVACI", r"davac(?:ı|i|ısı|ına)"),
    ("DAVALI", r"daval(?:ı|i|ısı|ına)"),
    ("MÜDAFİİ", r"müdaf(?:ii|i)"),
    ("VEKİL", r"vek(?:il|ili)"),
    ("TANIK", r"tanık"),
    ("BİLİRKİŞİ", r"bilirkişi"),
]


@dataclass
class MaskResult:
    masked_text: str
    manifest: Dict[str, str] = field(default_factory=dict)  # maske -> gerçek

    def restore(self, text: str) -> str:
        for mask, real in self.manifest.items():
            text = text.replace(mask, real)
        return text


def mask_pii(text: str, salt: str = "yerli-dilekce") -> MaskResult:
    manifest: Dict[str, str] = {}
    counters: Dict[str, int] = {}

    def _make(kind: str, real: str) -> str:
        if real in manifest.values():
            for m, v in manifest.items():
                if v == real:
                    return m
        counters[kind] = counters.get(kind, 0) + 1
        tag = f"[{kind}_{counters[kind]}]"
        manifest[tag] = real
        return tag

    masked = text
    for kind, pattern, _desc in _PATTERNS:
        rx = re.compile(pattern)
        def repl(m, kind=kind):
            return _make(kind, m.group(0))
        masked = rx.sub(repl, masked)

    # rol ipuçlı isim öbekleri (önce roller, kimlik belirsizse sayısal etiket)
    for role, hint in _ROLE_HINTS:
        pass  # roller metinde kalabilir; isim maskesi yeterli

    def name_repl(m):
        cand = m.group(1).strip()
        if len(cand.split()) < 2:
            return m.group(0)
        # sık yanlış pozitifler: ay/tarih/kanun adları korunsun
        if cand.split()[0] in {"Türkiye", "Ankara", "İstanbul", "Kanun", "Madde", "Sayın"}:
            return m.group(0)
        return _make("KISI", cand)

    masked = _NAME_RE.sub(name_repl, masked)

    return MaskResult(masked_text=masked, manifest=manifest)


def mask_file(src: str, dst_manifest: str = "") -> MaskResult:
    p = Path(src)
    text = p.read_text(encoding="utf-8")
    res = mask_pii(text)
    if dst_manifest:
        Path(dst_manifest).write_text(
            json.dumps(res.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return res
