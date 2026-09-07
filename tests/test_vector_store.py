# -*- coding: utf-8 -*-
"""Tests for VectorStore: similarity search, hybrid ranking, persistence."""

import numpy as np
import pytest

from semantic_search.vector_store import Document, VectorStore


def _vec(*values) -> np.ndarray:
    v = np.array(values, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-12)


@pytest.fixture
def store():
    s = VectorStore(dimension=3)
    s.add_documents(
        ids=["d1", "d2", "d3"],
        texts=["muris muvazaası", "finansal kiralama", "marka lisansı"],
        embeddings=np.array([_vec(1, 0, 0), _vec(0, 1, 0), _vec(0, 0, 1)]),
        metadata=[{"court_type": "YARGITAYKARARI"}, {"court_type": "YERELHUKUK"}, {"court_type": "YARGITAYKARARI"}],
    )
    return s


def test_size_and_get_by_id(store):
    assert store.size() == 3
    doc = store.get_by_id("d2")
    assert doc is not None
    assert doc.text == "finansal kiralama"
    assert store.get_by_id("missing") is None


def test_search_returns_sorted_top_k(store):
    results = store.search(_vec(0, 1, 0), top_k=2)
    assert len(results) == 2
    assert results[0][0].id == "d2"
    assert abs(results[0][1] - 1.0) < 1e-4
    assert results[1][0].id in ("d1", "d3")


def test_search_threshold(store):
    results = store.search(_vec(0, 1, 0), top_k=10, threshold=0.99)
    assert [r[0].id for r in results] == ["d2"]


def test_search_empty_store():
    s = VectorStore(dimension=3)
    assert s.search(_vec(1, 0, 0)) == []


def test_search_single_document_store():
    # Regression: squeeze() used to collapse a (1, dim) dot product to a
    # 0-d scalar, crashing len() in the top-k selection.
    s = VectorStore(dimension=3)
    s.add_documents(["d1"], ["text"], np.array([_vec(1, 0, 0)]))
    results = s.search(_vec(1, 0, 0), top_k=1)
    assert len(results) == 1
    assert results[0][0].id == "d1"
    assert abs(results[0][1] - 1.0) < 1e-4


def test_hybrid_search_blends_scores(store):
    keyword_scores = {"d1": 1.0, "d2": 0.0, "d3": 0.0}
    results = store.hybrid_search(_vec(0, 1, 0), keyword_scores, top_k=3, alpha=0.5)
    # d2 is the semantic winner but gets no keyword boost; d1 has keyword boost
    scores = {d.id: score for d, score in results}
    assert scores["d2"] == pytest.approx(0.5)
    assert scores["d1"] == pytest.approx(0.5)


def test_hybrid_search_empty_keyword_scores_no_crash(store):
    # Regression: max() on an empty dict used to raise ValueError
    results = store.hybrid_search(_vec(0, 1, 0), {}, top_k=3, alpha=0.6)
    assert len(results) == 3
    assert results[0][0].id == "d2"


def test_hybrid_search_normalizes_scores_above_one(store):
    results = store.hybrid_search(_vec(1, 0, 0), {"d1": 5.0}, top_k=3, alpha=0.5)
    scores = {d.id: score for d, score in results}
    assert scores["d1"] == pytest.approx(1.0)


def test_save_load_round_trip(store, tmp_path):
    store.save(str(tmp_path))
    loaded = VectorStore.load(str(tmp_path))
    assert loaded.size() == 3
    assert loaded.dimension == 3
    doc = loaded.get_by_id("d2")
    assert doc.metadata == {"court_type": "YERELHUKUK"}
    results = loaded.search(_vec(0, 1, 0), top_k=1)
    assert results[0][0].id == "d2"


def test_load_missing_directory_returns_empty(tmp_path):
    store = VectorStore.load(str(tmp_path / "nope"))
    assert store.size() == 0


def test_load_corrupted_index_raises(tmp_path):
    store = VectorStore(dimension=3)
    store.add_documents(["a"], ["text"], np.array([_vec(1, 0, 0)]))
    store.save(str(tmp_path))
    # Corrupt: drop one row from the embeddings matrix
    import json as _json
    with open(tmp_path / "documents.json", "r", encoding="utf-8") as f:
        docs = _json.load(f)
    docs.append(docs[0])
    with open(tmp_path / "documents.json", "w", encoding="utf-8") as f:
        _json.dump(docs, f, ensure_ascii=False)
    with pytest.raises(ValueError):
        VectorStore.load(str(tmp_path))


def test_embeddings_round_trip_exact(store, tmp_path):
    store.save(str(tmp_path))
    loaded = VectorStore.load(str(tmp_path))
    assert np.allclose(store.embeddings, loaded.embeddings)


def test_document_to_dict_excludes_embedding():
    doc = Document(id="x", text="t", embedding=_vec(1, 0, 0), metadata={"k": "v"})
    d = doc.to_dict()
    assert d == {"id": "x", "text": "t", "metadata": {"k": "v"}}
