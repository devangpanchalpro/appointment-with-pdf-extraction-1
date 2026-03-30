import numpy as np
from typing import Dict, List
from sentence_transformers import SentenceTransformer
import faiss

from app.config.settings import settings
from google.genai import types

# Global cache for the local model to avoid redundant loads across imports
_local_embed_model = None

def get_embedding_model():
    """Lazy-load the local embedding model."""
    global _local_embed_model
    if _local_embed_model is None:
        print("🔄 Loading local embedding model (SentenceTransformer)...")
        _local_embed_model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
        print("✅ Local embedding model ready!")
    return _local_embed_model


class RAGStore:

    def __init__(self):
        self.dim     = 768 if settings.USE_GEMINI_EMBEDDINGS else 384
        self.indexes: Dict[str, faiss.IndexFlatIP] = {}
        self.chunks:  Dict[str, List[dict]]         = {}

    def _ensure(self, abha: str):
        if abha not in self.indexes:
            self.indexes[abha] = faiss.IndexFlatIP(self.dim)
            self.chunks[abha]  = []

    def _get_embeddings(self, texts: List[str]) -> np.ndarray:
        """Get embeddings using either Gemini (API) or SentenceTransformer (Local)."""
        if settings.USE_GEMINI_EMBEDDINGS:
            from medical_qa.config import client
            try:
                # Batch embed via Gemini
                res = client.models.embed_content(
                    model="text-embedding-004",
                    contents=texts,
                    config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT")
                )
                return np.array([e.values for e in res.embeddings], dtype=np.float32)
            except Exception as e:
                print(f"⚠️ Gemini embedding failed, falling back to local: {e}")
                # Fallback to local if Gemini fails
        
        # Local fallback/default
        model = get_embedding_model()
        return np.array(model.encode(texts, normalize_embeddings=True), dtype=np.float32)

    def _split(self, text: str, size=500, overlap=80) -> List[str]:
        words = text.split()
        out = []
        i = 0
        while i < len(words):
            c = " ".join(words[i:i+size])
            if c.strip():
                out.append(c)
            i += size - overlap
        return out

    def add(self, text: str, filename: str, abha: str, page=None):
        self._ensure(abha)
        chunks = self._split(text)
        if not chunks:
            return
        
        embs = self._get_embeddings(chunks)
        self.indexes[abha].add(embs)
        for i, c in enumerate(chunks):
            self.chunks[abha].append({
                "text": c,
                "meta": {"filename": filename, "page": page, "chunk": i}
            })

    def search(self, query: str, abha: str, top_k: int = 7) -> List[dict]:
        self._ensure(abha)
        idx = self.indexes[abha]
        if idx.ntotal == 0:
            return []
        
        q = self._get_embeddings([query])
        k = min(top_k, idx.ntotal)
        _, ids = idx.search(q, k)
        return [self.chunks[abha][i] for i in ids[0] if i < len(self.chunks[abha])]

    def files(self, abha: str) -> List[str]:
        self._ensure(abha)
        return list({c["meta"]["filename"] for c in self.chunks.get(abha, [])})

    def doc_count(self, abha: str) -> int:
        return len(self.files(abha))

    def clear(self, abha: str):
        self.indexes[abha] = faiss.IndexFlatIP(self.dim)
        self.chunks[abha]  = []


rag = RAGStore()