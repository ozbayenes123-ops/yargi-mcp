# -*- coding: utf-8 -*-
"""Tests for SemanticCorpus: persistent per-model vector corpus."""

import numpy as np
import pytest

from semantic_search.corpus import SemanticCorpus


def _vec(*values) -> np.ndarray:
    v = np.array(values, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-12)


def _embeddings(n: int, dim: int = 3) -> np.ndarray:
    rng = np.random.default_rng(7)
    out = rng.normal(size=(n, dim)).astype(np.float32)
    return out / (np.linalg.norm(out, axis=1, keepdims=True) + 1e-9)


def test_add_documents_skips_duplicates(tmp_path):
    corpus = SemanticCorpus(model="test-model", dimension=3, index_dir=str(tmp_path))
    emb = _embeddings(2)
    added = corpus.add_documents(
        ids=["a", "b"],
        texts=["text a", "text b"],
        embeddings=emb,
        metadata=[{"court_type": "YARGITAYKARARI"}, {"court_type": "YERELHUKUK"}],
    )
    assert added == 2
    assert corpus.size == 2

    added_again = corpus.add_documents(
        ids=["a", "c"],
        texts=["text a", "text c"],
        embeddings=_embeddings(2),
    )
    assert added_again == 1  # only 'c' is new
    assert corpus.size == 3
    assert corpus.has("a")
    assert not corpus.has("z")


def test_persistence_across_instances(tmp_path):
    corpus = SemanticCorpus(model="test-model", dimension=3, index_dir=str(tmp_path))
    corpus.add_documents(["a"], ["text a"], _embeddings(1))
    assert corpus.size == 1

    # A fresh instance in the same directory reloads from disk
    reloaded = SemanticCorpus(model="test-model", dimension=3, index_dir=str(tmp_path))
    assert reloaded.size == 1
    assert reloaded.has("a")
    assert reloaded.get("a").text == "text a"


def test_per_model_subdirectories(tmp_path):
    corpus_a = SemanticCorpus(model="model-a", dimension=3, index_dir=str(tmp_path))
    corpus_b = SemanticCorpus(model="model-b", dimension=3, index_dir=str(tmp_path))
    corpus_a.add_documents(["a"], ["text a"], _embeddings(1))
    corpus_b.add_documents(["b"], ["text b"], _embeddings(1))
    assert corpus_a.has("a") and not corpus_a.has("b")
    assert corpus_b.has("b") and not corpus_b.has("a")


def test_dimension_mismatch_rebuilds_empty(tmp_path):
    corpus = SemanticCorpus(model="test-model", dimension=3, index_dir=str(tmp_path))
    corpus.add_documents(["a"], ["text a"], _embeddings(1))
    assert corpus.size == 1

    # Same model dir but different dimension -> incompatible, rebuild empty
    wrong = SemanticCorpus(model="test-model", dimension=128, index_dir=str(tmp_path))
    assert wrong.size == 0


def test_search_and_hybrid(tmp_path):
    corpus = SemanticCorpus(model="test-model", dimension=3, index_dir=str(tmp_path))
    emb = np.array([_vec(1, 0, 0), _vec(0, 1, 0), _vec(0, 0, 1)])
    corpus.add_documents(
        ids=["d1", "d2", "d3"],
        texts=["a", "b", "c"],
        embeddings=emb,
    )
    results = corpus.search(_vec(0, 1, 0), top_k=1)
    assert results[0][0].id == "d2"

    hybrid = corpus.hybrid_search(_vec(0, 1, 0), {"d1": 1.0}, top_k=2, alpha=0.5)
    scores = {d.id: s for d, s in hybrid}
    assert scores["d2"] == pytest.approx(0.5)
    assert scores["d1"] == pytest.approx(0.5)


def test_stats_shape(tmp_path):
    corpus = SemanticCorpus(model="test-model", dimension=3, index_dir=str(tmp_path))
    corpus.add_documents(["a"], ["text a"], _embeddings(1))
    stats = corpus.stats()
    assert stats["num_documents"] == 1
    assert stats["model"] == "test-model"
    assert "index_dir" in stats
    assert stats["dimension"] == 3


def test_model_slug_sanitization(tmp_path):
    corpus = SemanticCorpus(model="foo/bar baz", dimension=3, index_dir=str(tmp_path))
    assert "foo/bar baz" not in corpus.index_dir
    assert "foo" in corpus.index_dir
