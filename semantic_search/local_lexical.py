# -*- coding: utf-8 -*-
"""
Tamamen yerel, dış-ağızsız (offline, zero-API) semantik arama çekirdeği.

Kullanılan teknikler (hepsi saf Python + numpy, hiçbir model indirme yok):
1. Türkçe duyarlı normalizasyon + kök kırpma (çekim eklerini atma)
2. BM25 anahtar kelime skoru (IDF ağırlıklı)
3. Kelime birliktelik vektörleri: her kelime, aynı belgede yan yana geçtiği
   kelimelerin ağırlıklı toplamı ile temsil edilir (PMI-benzeri), böylece
   "tapu iptali" ile "mülkiyet devri" gibi farklı kelimeler aynı belgede
   birlikte geçtiği için yakınsar.
4. Sorgu ile belge arasındaki kosinüs benzerliği.

Vektör matrisleri diske önbelleklenir (SEMANTIC_LEXICAL_CACHE);
indeks kullandıkça büyür. deterministiktir, ağ kullanmaz.
"""

import logging
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = os.environ.get("SEMANTIC_LEXICAL_CACHE", "data/lexical_index")

# Türkçe sık ekler — kök kırpma için (aşırı agresif değil, 2+ harfli)
_SUFFIXES = [
    "larindan", "larindaki", "larinda", "larini", "larinin", "larina", "lar", "larindan",
    "lerinden", "lerindeki", "lerinde", "lerini", "lerinin", "lerine", "ler",
    "masindan", "masindaki", "masinda", "masini", "masinin", "masina", "masi",
    "mesinden", "mesindeki", "mesinde", "mesini", "mesinin", "mesine", "mesi",
    "malarindan", "malarinin", "masinin",
    "makta", "mekte", "misiz", "mistir", "musuz", "mustur", "mistir", "mustur",
    "iyorlar", "iyorum", "iyoruz", "iyor", "iyorlar", "ıyor", "ıyorlar",
    "arak", "erek", "ince", "ince", "alari", "aleri",
    "tir", "tur", "tir", "tir", "dir", "dur", "tir",
    "sin", "sun", "sina", "sine", "sini", "sinin",
    "dan", "den", "tan", "ten", "da", "de", "ta", "te",
    "nin", "nun", "nun", "nin", "nu", "ni", "nun",
    "yla", "yle", "yla", "ile",
    "lar", "ler", "ma", "me", "ki", "ca", "ce",
    "li", "lu", "li", "lü",
]
_SUFFIXES = sorted(set(_SUFFIXES), key=len, reverse=True)

_TR_MAP = str.maketrans({
    "İ": "i", "I": "i", "ı": "i", "İ": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
    "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c",
    "Â": "a", "â": "a", "Î": "i", "î": "i", "Û": "u", "û": "u",
})

_STOPWORDS = {
    "ve", "ile", "bir", "bu", "su", "o", "ki", "da", "de", "mi", "mu", "mı", "mu",
    "icin", "gibi", "daha", "cok", "az", "her", "hic", "ne", "ya", "yada", "veya",
    "ama", "fakat", "ancak", "cunku", "eger", "ise", "olan", "olarak", "oldugu",
    "olan", "sonra", "once", "kadar", "dolayi", "dogru", "uzere", "tarafindan",
    "ben", "sen", "biz", "siz", "onlar", "bunlar", "su", "ise", "yani", "arti",
}


def _norm(word: str) -> str:
    w = word.translate(_TR_MAP).lower()
    w = re.sub(r"[^a-z0-9]", "", w)
    return w


def _stem(norm_word: str) -> str:
    """Basit kök kırpma: uzun ekleri at, çok kısalmadan dur."""
    w = norm_word
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[: -len(suf)]
            break
    return w


def tokenize(text: str) -> List[str]:
    out = []
    for raw in re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü0-9']+", text):
        n = _norm(raw)
        if len(n) < 2 or n in _STOPWORDS:
            continue
        out.append(_stem(n))
    return out


class LexicalSemanticIndex:
    """BM25 + birliktelik vektörleri ile yerel semantik sıralama."""

    def __init__(self, cache_dir: Optional[str] = None, assoc_dim: int = 256):
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.assoc_dim = assoc_dim
        self.doc_tokens: Dict[str, List[str]] = {}
        self.doc_meta: Dict[str, Dict] = {}
        self.df: Counter = Counter()
        self.assoc: Dict[str, np.ndarray] = {}
        self._dirty = False

    # ---------- indeksleme ----------
    def add_document(self, doc_id: str, text: str, meta: Optional[Dict] = None) -> None:
        toks = tokenize(text)
        self.doc_tokens[doc_id] = toks
        self.doc_meta[doc_id] = meta or {}
        for t in set(toks):
            self.df[t] += 1
        self._dirty = True

    def build_associations(self) -> None:
        """Kelime-kelime birlikteliğini say, PMI-benzeri ağırlıkla vektörleştir."""
        co: Dict[str, Counter] = defaultdict(Counter)
        tf: Counter = Counter()
        for toks in self.doc_tokens.values():
            uniq = set(toks)
            for t in toks:
                tf[t] += 1
            for a in uniq:
                for b in uniq:
                    if a != b:
                        co[a][b] += 1
        vocab = [t for t, c in tf.items() if c >= 2]
        rng = np.random.RandomState(13)
        proj = {t: rng.normal(size=self.assoc_dim).astype(np.float32) for t in vocab}
        self.assoc = {}
        for t in vocab:
            acc = np.zeros(self.assoc_dim, dtype=np.float32)
            tot = 0.0
            for nb, cnt in co[t].items():
                if nb in proj:
                    w = math.log(1.0 + cnt) / math.sqrt(tf[t] * tf[nb])
                    acc += w * proj[nb]
                    tot += w
            if tot > 0:
                acc /= tot
            n = np.linalg.norm(acc)
            self.assoc[t] = (acc / n) if n > 0 else acc
        self._dirty = False
        logger.info("lexical associations built: %d terms", len(self.assoc))

    # ---------- sorgulama ----------
    def _bm25(self, qtoks: Sequence[str], doc_id: str, k1: float = 1.4, b: float = 0.75) -> float:
        toks = self.doc_tokens[doc_id]
        if not toks:
            return 0.0
        tf = Counter(toks)
        n_docs = max(1, len(self.doc_tokens))
        avg_len = sum(len(v) for v in self.doc_tokens.values()) / n_docs
        score = 0.0
        for q in qtoks:
            if q not in tf:
                continue
            df = max(1, self.df.get(q, 1))
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            denom = tf[q] + k1 * (1 - b + b * len(toks) / max(1.0, avg_len))
            score += idf * tf[q] * (k1 + 1) / denom
        return score

    def _semantic_vec(self, qtoks: Sequence[str]) -> Optional[np.ndarray]:
        acc = np.zeros(self.assoc_dim, dtype=np.float32)
        hits = 0
        for q in qtoks:
            if q in self.assoc:
                acc += self.assoc[q]
                hits += 1
        if hits == 0:
            return None
        acc /= hits
        n = np.linalg.norm(acc)
        return (acc / n) if n > 0 else acc

    def search(
        self,
        query: str,
        candidates: Optional[Iterable[str]] = None,
        top_k: int = 10,
        alpha: float = 0.5,
    ) -> List[Tuple[str, float, Dict]]:
        """
        alpha = 0 saf BM25, alpha = 1 saf semantik birliktelik;
        aday verilmezse tümü taranır.
        """
        if self._dirty:
            self.build_associations()
        qtoks = tokenize(query)
        qvec = self._semantic_vec(qtoks)
        ids = list(candidates) if candidates is not None else list(self.doc_tokens)
        bm_scores = [(i, self._bm25(qtoks, i)) for i in ids if i in self.doc_tokens]
        max_bm = max((s for _, s in bm_scores), default=0.0) or 1.0
        results = []
        for doc_id, bm in bm_scores:
            sem = 0.0
            if qvec is not None:
                dtoks = self.doc_tokens[doc_id]
                acc = np.zeros(self.assoc_dim, dtype=np.float32)
                hits = 0
                for t in set(dtoks):
                    if t in self.assoc:
                        acc += self.assoc[t]
                        hits += 1
                if hits:
                    acc /= hits
                    n = np.linalg.norm(acc)
                    if n > 0:
                        sem = float(np.dot(qvec, acc / n))
            score = (1 - alpha) * (bm / max_bm) + alpha * max(0.0, sem)
            results.append((doc_id, score, self.doc_meta.get(doc_id, {})))
        results.sort(key=lambda r: -r[1])
        return results[:top_k]

    # ---------- kalıcılık ----------
    def save(self) -> None:
        np.savez_compressed(
            self.cache_dir / "assoc.npz",
            **{k: v for k, v in self.assoc.items()},
        )
        import json
        (self.cache_dir / "docs.json").write_text(
            json.dumps({"tokens": self.doc_tokens, "meta": self.doc_meta,
                        "df": dict(self.df)}, ensure_ascii=False),
            encoding="utf-8",
        )

    def load(self) -> bool:
        import json
        p = self.cache_dir / "docs.json"
        if not p.exists():
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        self.doc_tokens = {k: list(v) for k, v in data["tokens"].items()}
        self.doc_meta = data.get("meta", {})
        self.df = Counter(data.get("df", {}))
        npz = self.cache_dir / "assoc.npz"
        if npz.exists():
            z = np.load(npz)
            self.assoc = {k: z[k] for k in z.files}
        self._dirty = False
        return True
