# semantic_search/vector_store.py

import logging
import numpy as np
import os
import json
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class Document:
    """Represents a document with its embedding and metadata."""
    id: str
    text: str
    embedding: np.ndarray
    metadata: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (excluding embedding for serialization)."""
        return {
            'id': self.id,
            'text': self.text,
            'metadata': self.metadata
        }

class VectorStore:
    """
    In-memory vector storage with similarity search capabilities.
    Future versions can use Faiss, ChromaDB, or other vector databases.
    """
    
    def __init__(self, dimension: int = 768):
        """
        Initialize vector store.
        
        Args:
            dimension: Embedding dimension size
        """
        self.dimension = dimension
        self.documents: List[Document] = []
        self.embeddings: Optional[np.ndarray] = None
        self.index_built = False
        
        logger.info(f"Initialized VectorStore with dimension: {dimension}")
    
    def add_documents(self, 
                     ids: List[str],
                     texts: List[str],
                     embeddings: np.ndarray,
                     metadata: Optional[List[Dict[str, Any]]] = None) -> int:
        """
        Add documents to the vector store.
        
        Args:
            ids: Document IDs
            texts: Document texts
            embeddings: Document embeddings (N x dimension)
            metadata: Optional metadata for each document
            
        Returns:
            Number of documents added
        """
        if len(ids) != len(texts) or len(ids) != embeddings.shape[0]:
            raise ValueError("Mismatched lengths for ids, texts, and embeddings")
        
        if embeddings.ndim != 2 or embeddings.shape[1] != self.dimension:
            raise ValueError(
                f"Embedding dimension mismatch: store dimension={self.dimension}, "
                f"got shape={embeddings.shape}"
            )
        
        if metadata and len(metadata) != len(ids):
            raise ValueError("Metadata length doesn't match document count")
        
        # Add documents
        for i in range(len(ids)):
            doc = Document(
                id=ids[i],
                text=texts[i],
                embedding=embeddings[i],
                metadata=metadata[i] if metadata else {}
            )
            self.documents.append(doc)
        
        # Rebuild index
        self._build_index()
        
        logger.info(f"Added {len(ids)} documents to vector store. Total: {len(self.documents)}")
        return len(ids)
    
    def _build_index(self):
        """Build or rebuild the embedding index."""
        if not self.documents:
            self.embeddings = None
            self.index_built = False
            return
        
        # Stack all embeddings into a single array
        self.embeddings = np.vstack([doc.embedding for doc in self.documents])
        self.index_built = True
        
        logger.debug(f"Built index with shape: {self.embeddings.shape}")
    
    def search(self, 
              query_embedding: np.ndarray,
              top_k: int = 10,
              threshold: Optional[float] = None) -> List[Tuple[Document, float]]:
        """
        Search for similar documents using cosine similarity.
        
        Args:
            query_embedding: Query embedding vector
            top_k: Number of results to return
            threshold: Optional similarity threshold (0-1)
            
        Returns:
            List of (Document, similarity_score) tuples
        """
        if not self.index_built or self.embeddings is None:
            logger.warning("No documents in vector store")
            return []
        
        # Ensure query is 2D
        if len(query_embedding.shape) == 1:
            query_embedding = query_embedding.reshape(1, -1)
        
        # Compute cosine similarities (assuming normalized embeddings)
        similarities = np.dot(self.embeddings, query_embedding.T).squeeze()
        # With a single document, squeeze() collapses to a 0-d scalar.
        similarities = np.atleast_1d(similarities)
        
        # Apply threshold if specified
        if threshold is not None:
            valid_indices = np.where(similarities >= threshold)[0]
            if len(valid_indices) == 0:
                logger.info(f"No documents above threshold {threshold}")
                return []
            similarities = similarities[valid_indices]
            valid_docs = [self.documents[i] for i in valid_indices]
        else:
            valid_docs = self.documents
        
        # Get top-k indices
        top_k = min(top_k, len(valid_docs))
        if top_k == 0:
            return []
        
        # Use argpartition for efficiency with large arrays
        if len(similarities) > top_k:
            top_indices = np.argpartition(similarities, -top_k)[-top_k:]
            top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]
        else:
            top_indices = np.argsort(similarities)[::-1]
        
        # Create results
        results = []
        for idx in top_indices:
            doc = valid_docs[idx] if threshold else self.documents[idx]
            score = float(similarities[idx])
            results.append((doc, score))
        
        logger.info(f"Search returned {len(results)} results (top_k={top_k})")
        return results
    
    def hybrid_search(self,
                     query_embedding: np.ndarray,
                     keyword_scores: Dict[str, float],
                     top_k: int = 10,
                     alpha: float = 0.5) -> List[Tuple[Document, float]]:
        """
        Hybrid search combining vector similarity and keyword scores.
        
        Args:
            query_embedding: Query embedding vector
            keyword_scores: Document ID to keyword relevance score mapping
            top_k: Number of results to return
            alpha: Weight for vector similarity (1-alpha for keyword score)
            
        Returns:
            List of (Document, combined_score) tuples
        """
        if not self.index_built:
            logger.warning("No documents in vector store")
            return []

        # Get vector similarities
        vector_results = self.search(query_embedding, top_k=len(self.documents))

        # Normalize keyword scores to 0-1 if needed
        max_keyword = max(keyword_scores.values()) if keyword_scores else 0.0
        if max_keyword > 1.0:
            keyword_scores = {k: v / max_keyword for k, v in keyword_scores.items()}

        # Combine scores
        combined_scores = []
        for doc, vector_score in vector_results:
            keyword_score = keyword_scores.get(doc.id, 0.0)

            combined_score = alpha * vector_score + (1 - alpha) * keyword_score
            combined_scores.append((doc, combined_score))
        
        # Sort by combined score and return top-k
        combined_scores.sort(key=lambda x: x[1], reverse=True)
        results = combined_scores[:top_k]
        
        logger.info(f"Hybrid search returned {len(results)} results")
        return results
    
    def clear(self):
        """Clear all documents from the store."""
        self.documents = []
        self.embeddings = None
        self.index_built = False
        logger.info("Cleared vector store")
    
    def size(self) -> int:
        """Get number of documents in store."""
        return len(self.documents)
    
    def get_by_id(self, doc_id: str) -> Optional[Document]:
        """Get document by ID."""
        for doc in self.documents:
            if doc.id == doc_id:
                return doc
        return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the vector store."""
        stats = {
            'num_documents': len(self.documents),
            'dimension': self.dimension,
            'index_built': self.index_built,
            'memory_usage_mb': 0
        }
        
        if self.embeddings is not None:
            # Estimate memory usage
            memory_bytes = self.embeddings.nbytes
            for doc in self.documents:
                memory_bytes += len(doc.text.encode('utf-8'))
                memory_bytes += len(json.dumps(doc.metadata).encode('utf-8'))
            stats['memory_usage_mb'] = memory_bytes / (1024 * 1024)
        
        return stats

    # ------------------------------------------------------------------
    # Persistence (disk-backed corpus, Pinecone-free alternative)
    # ------------------------------------------------------------------

    def save(self, directory: str) -> None:
        """
        Persist the store to ``directory`` as ``documents.json`` (metadata +
        text) and ``embeddings.npy`` (dense matrix). Loading later restores
        an identical in-memory index.
        """
        os.makedirs(directory, exist_ok=True)
        docs = [
            {"id": d.id, "text": d.text, "metadata": d.metadata}
            for d in self.documents
        ]
        with open(os.path.join(directory, "documents.json"), "w", encoding="utf-8") as f:
            json.dump(docs, f, ensure_ascii=False)
        if self.embeddings is not None:
            np.save(os.path.join(directory, "embeddings.npy"), self.embeddings)
        logger.info(
            "VectorStore saved to %s (%d documents, dim=%d)",
            directory, len(docs), self.dimension,
        )

    @classmethod
    def load(cls, directory: str) -> "VectorStore":
        """
        Load a previously saved store from ``directory``. Returns an empty
        store when nothing is persisted there. Raises ValueError if the
        stored embedding dimension differs from the caller's expectation
        (caller should then clear or rebuild the index).
        """
        docs_path = os.path.join(directory, "documents.json")
        emb_path = os.path.join(directory, "embeddings.npy")
        if not os.path.exists(docs_path) or not os.path.exists(emb_path):
            logger.info("No persisted vector store at %s; starting empty", directory)
            return cls()

        with open(docs_path, "r", encoding="utf-8") as f:
            docs = json.load(f)
        embeddings = np.load(emb_path)

        if len(docs) != embeddings.shape[0]:
            raise ValueError(
                f"Corrupted index at {directory}: {len(docs)} documents but "
                f"embeddings matrix has {embeddings.shape[0]} rows."
            )

        store = cls(dimension=embeddings.shape[1])
        store.documents = [
            Document(
                id=d["id"],
                text=d["text"],
                embedding=embeddings[i],
                metadata=d.get("metadata", {}),
            )
            for i, d in enumerate(docs)
        ]
        store.embeddings = embeddings
        store.index_built = True
        logger.info(
            "VectorStore loaded from %s (%d documents, dim=%d)",
            directory, len(store.documents), store.dimension,
        )
        return store