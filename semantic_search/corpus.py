# semantic_search/corpus.py
# -*- coding: utf-8 -*-

"""
Persistent semantic corpus manager.

Alternative to a hosted vector database (e.g. Pinecone): embeddings and
document texts are stored on disk (``SEMANTIC_INDEX_DIR``) per embedding
model, so the corpus grows over time as searches run. A document fetched
once is embedded once; later searches reuse the cached vector instead of
re-fetching and re-embedding.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .vector_store import Document, VectorStore

logger = logging.getLogger(__name__)

DEFAULT_INDEX_DIR = "data/semantic_index"


def _model_slug(model_name: str) -> str:
    """Sanitize a model name into a safe directory name."""
    slug = re.sub(r"[^a-zA-Z0-9._-]", "-", model_name or "default")
    return slug.strip("-") or "default"


class SemanticCorpus:
    """
    Disk-backed vector corpus.

    One sub-directory per embedding model so that switching models never
    mixes incompatible vector spaces. Documents are upserted by id; the
    underlying :class:`VectorStore` rebuilds its index on every add, which
    is fine up to a few thousand documents.
    """

    def __init__(self, model: str, dimension: int, index_dir: Optional[str] = None):
        self.model = model
        self.dimension = dimension
        self.index_dir = os.path.join(
            index_dir or os.getenv("SEMANTIC_INDEX_DIR", DEFAULT_INDEX_DIR),
            _model_slug(model),
        )
        self.store = self._load()

    def _load(self) -> VectorStore:
        try:
            store = VectorStore.load(self.index_dir)
        except ValueError as e:
            logger.warning("Rebuilding incompatible index at %s: %s", self.index_dir, e)
            return VectorStore(dimension=self.dimension)
        if store.size() == 0:
            # Nothing persisted: start with the expected dimension instead of
            # the VectorStore default (768), which would poison later searches.
            return VectorStore(dimension=self.dimension)
        if store.dimension != self.dimension:
            logger.warning(
                "Rebuilding index at %s: stored dimension %d != expected %d",
                self.index_dir, store.dimension, self.dimension,
            )
            return VectorStore(dimension=self.dimension)
        return store

    @property
    def size(self) -> int:
        return self.store.size()

    def has(self, doc_id: str) -> bool:
        return self.store.get_by_id(doc_id) is not None

    def add_documents(
        self,
        ids: List[str],
        texts: List[str],
        embeddings: np.ndarray,
        metadata: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """
        Upsert documents, skipping ids already present. Persists immediately.
        """
        if not ids:
            return 0
        new_idx = [i for i, d_id in enumerate(ids) if not self.has(d_id)]
        if not new_idx:
            return 0
        n_ids = [ids[i] for i in new_idx]
        n_texts = [texts[i] for i in new_idx]
        n_embs = embeddings[new_idx]
        n_meta = (
            [metadata[i] for i in new_idx] if metadata is not None else None
        )
        added = self.store.add_documents(n_ids, n_texts, n_embs, n_meta)
        self.store.save(self.index_dir)
        logger.info("SemanticCorpus: added %d documents (total=%d)", added, self.size)
        return added

    def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 10,
        threshold: Optional[float] = None,
    ) -> List[Tuple[Document, float]]:
        return self.store.search(query_embedding, top_k=top_k, threshold=threshold)

    def hybrid_search(
        self,
        query_embedding: np.ndarray,
        keyword_scores: Dict[str, float],
        top_k: int = 10,
        alpha: float = 0.5,
    ) -> List[Tuple[Document, float]]:
        return self.store.hybrid_search(
            query_embedding, keyword_scores, top_k=top_k, alpha=alpha
        )

    def get(self, doc_id: str) -> Optional[Document]:
        return self.store.get_by_id(doc_id)

    def stats(self) -> Dict[str, Any]:
        stats = self.store.get_stats()
        stats["model"] = self.model
        stats["index_dir"] = self.index_dir
        return stats
