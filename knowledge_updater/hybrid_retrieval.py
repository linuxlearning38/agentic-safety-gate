"""
AVA Phase 3 — Hybrid Retrieval Module
hybrid_retrieval.py

Phase 3 additions:
  - assemble_context(): Groups chunks from same article and merges them
    Fixes the split-context problem — YAML fix and explanation were
    landing in separate chunks, AVA was only picking one.

Queries both ChromaDB collections and returns merged context.
Policies first (trusted), blogs second (dynamic).
Falls back to policies-only if devops_blogs_v1 doesn't exist yet.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import chromadb
import requests
from chromadb.config import Settings

logger = logging.getLogger("ava.hybrid_retrieval")


@dataclass
class RetrievedChunk:
    content: str
    source_collection: str
    distance: float
    metadata: dict = field(default_factory=dict)

    @property
    def relevance_score(self) -> float:
        # L2 distance — convert to 0-1 score using exponential decay
        return round(1.0 / (1.0 + self.distance), 4)

    @property
    def source_label(self) -> str:
        if self.source_collection == "policies":
            return f"[Policy] {self.metadata.get('source', 'mrcloudbook.com')}"
        else:
            return f"[Blog] {self.metadata.get('source', 'Unknown')} — {self.metadata.get('title', '')[:50]}"


import os
class EmbeddingClient:
    def __init__(self, ollama_url: str, model: str):
        ollama_url = os.getenv("OLLAMA_HOST", ollama_url)
        self.url = f"{ollama_url}/api/embeddings"
        self.model = model

    def embed(self, text: str) -> Optional[list]:
        try:
            resp = requests.post(
                self.url,
                json={"model": self.model, "prompt": text},
                timeout=15
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except Exception as e:
            logger.error(f"Embedding failed: {e}")
            return None


class HybridRetriever:
    def __init__(self, config: dict, existing_client=None):
        self.config = config
        self.embedder = EmbeddingClient(
            ollama_url=config["embedding"]["ollama_url"],
            model=config["embedding"]["model"]
        )

        # Use existing client if provided, else create new one
        if existing_client is not None:
            self.client = existing_client
        else:
            self.client = chromadb.PersistentClient(
                path=config["chromadb"]["persist_directory"],
                settings=Settings(anonymized_telemetry=False)
            )

        self.policies_collection = self._get_collection(
            config["chromadb"]["policies_collection"],
            required=True
        )
        self.blogs_collection = self._get_collection(
            config["chromadb"]["blogs_collection"],
            required=False
        )

    def _get_collection(self, name: str, required: bool = True):
        try:
            collection = self.client.get_collection(name=name)
            logger.info(f"Loaded collection '{name}' ({collection.count()} chunks)")
            return collection
        except Exception as e:
            if required:
                logger.error(f"Required collection '{name}' not found: {e}")
                raise
            else:
                logger.info(f"Optional collection '{name}' not found — policies only mode")
                return None

    def _query_collection(self, collection, embedding: list, n_results: int, label: str) -> list:
        if not collection:
            return []
        try:
            results = collection.query(
                query_embeddings=[embedding],
                n_results=min(n_results, collection.count()),
                include=["documents", "metadatas", "distances"]
            )
            chunks = []
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0]
            ):
                chunks.append(RetrievedChunk(
                    content=doc,
                    source_collection=label,
                    distance=float(dist),
                    metadata=meta or {}
                ))
            return chunks
        except Exception as e:
            logger.error(f"Query failed ({label}): {e}")
            return []

    def _keyword_fetch(self, collection, keywords: list, limit: int = 5) -> list:
        """Directly fetch chunks containing specific keywords."""
        if not collection:
            return []
        try:
            all_results = collection.get(
                include=["documents", "metadatas"]
            )
            matched = []
            for doc, meta in zip(all_results["documents"], all_results["metadatas"]):
                if any(kw.lower() in doc.lower() for kw in keywords):
                    matched.append(RetrievedChunk(
                        content=doc,
                        source_collection="blogs",
                        distance=0.1,
                        metadata=meta or {}
                    ))
                if len(matched) >= limit:
                    break
            return matched
        except Exception as e:
            logger.error(f"Keyword fetch failed: {e}")
            return []

    def query(
        self,
        query_text: str,
        n_policies: int = 4,
        n_blogs: int = 3,
        blog_min_relevance: float = 0.45,
        format_for_llm: bool = True
    ):
        start = time.time()

        embedding = self.embedder.embed(query_text)
        if not embedding:
            return "" if format_for_llm else []

        policy_chunks = self._query_collection(
            self.policies_collection, embedding, n_policies, "policies"
        )
        blog_chunks = self._query_collection(
            self.blogs_collection, embedding, max(n_blogs, 20), "blogs"
        )

        # Keyword boost — chunks containing query words rank higher
        # Phase 3: expanded boost includes compound terms
        query_words = set(query_text.lower().split())
        BOOST_TERMS = [
            "fsGroupChangePolicy", "fsgroup", "persistentvolume", "pvc",
            "chown", "recursive", "securitycontext", "volumemount",
            "force-unlock", "state lock", "dynamodb", "disk pressure",
            "eviction", "kubelet", "imagegcthreshold", "nodefs",
            "OnRootMismatch", "securityContext", "600 hours", "cloudflare"
        ]
        # Extra heavy boost for fsGroupChangePolicy specifically
        for chunk in blog_chunks:
            if "fsGroupChangePolicy" in chunk.content or "OnRootMismatch" in chunk.content:
                chunk.distance = max(0, chunk.distance - 150)

        # Keyword-based forced injection for known high-value terms
        PV_KEYWORDS = ["persistentvolume", "pvc", "slow restart", "20 minutes", "30 minutes", "fsgroup"]
        FSGROUPCHANGE_KEYWORDS = ["fsGroupChangePolicy", "OnRootMismatch", "securityContext", "chown"]

        query_lower = query_text.lower()
        if any(kw in query_lower for kw in PV_KEYWORDS):
            forced = self._keyword_fetch(self.blogs_collection, FSGROUPCHANGE_KEYWORDS, limit=3)
            if forced:
                logger.info(f"Keyword injection: added {len(forced)} fsGroupChangePolicy chunks")
                blog_chunks = forced + blog_chunks
        for chunk in blog_chunks:
            keyword_hits = sum(1 for w in query_words if w in chunk.content.lower())
            # Extra boost for high-value technical terms
            bonus = sum(3 for term in BOOST_TERMS if term.lower() in chunk.content.lower())
            chunk.distance = max(0, chunk.distance - (keyword_hits * 5) - bonus)

        blog_chunks_filtered = [
            c for c in blog_chunks if c.relevance_score >= blog_min_relevance
        ]
        blog_chunks_filtered.sort(key=lambda x: x.relevance_score, reverse=True)

        elapsed = round((time.time() - start) * 1000, 1)
        logger.info(
            f"Hybrid retrieval: {len(policy_chunks)} policy + "
            f"{len(blog_chunks_filtered)} blog chunks in {elapsed}ms"
        )

        if not format_for_llm:
            return policy_chunks + blog_chunks_filtered

        return self._format_context(policy_chunks, blog_chunks_filtered)

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 3: Context Assembly
    # ─────────────────────────────────────────────────────────────────────────
    def assemble_context(
        self,
        chunks: list,
        max_articles: int = 4,
        max_chunks_per_article: int = 3
    ) -> list:
        """
        Group chunks from the same article/source and merge them into
        single coherent blocks before sending to the LLM.

        Problem this solves:
            The YAML config fix lives in chunk 7.
            The explanation of WHY lives in chunk 2.
            Without assembly, AVA picks only one of them.

        After assembly:
            Both chunks are merged into one block → AVA sees the complete picture.

        Args:
            chunks: List of RetrievedChunk objects from query()
            max_articles: Max number of unique sources to include
            max_chunks_per_article: Max chunks to merge from a single article

        Returns:
            List of merged text strings, ready to pass to generate_response()
        """
        if not chunks:
            return []

        grouped = {}
        # Preserve order — policies first, then blogs (chunks arrive ordered)
        for chunk in chunks:
            # Group by URL (same article = same URL)
            url = chunk.metadata.get("url", "")
            if not url:
                # Fallback: group by source name for policy chunks
                url = chunk.metadata.get("source", f"source_{id(chunk)}")

            if url not in grouped:
                grouped[url] = {
                    "parts": [],
                    "label": chunk.source_label,
                    "score": chunk.relevance_score
                }
            grouped[url]["parts"].append(chunk.content)

        # Build merged blocks, best-scoring articles first
        sorted_groups = sorted(
            grouped.items(),
            key=lambda x: x[1]["score"],
            reverse=True
        )

        merged = []
        for url, data in sorted_groups[:max_articles]:
            parts = data["parts"][:max_chunks_per_article]
            merged_text = "\n\n".join(parts)
            merged.append(merged_text)
            logger.debug(
                f"Assembled {len(parts)} chunks from: {url[:60]} "
                f"(score: {data['score']:.2%})"
            )

        logger.info(
            f"Context assembly: {len(chunks)} chunks → "
            f"{len(merged)} merged blocks from {len(grouped)} unique sources"
        )
        return merged

    def _format_context(self, policy_chunks: list, blog_chunks: list) -> str:
        sections = []

        if policy_chunks:
            sections.append("### Trusted Knowledge Base")
            for i, chunk in enumerate(policy_chunks, 1):
                sections.append(f"\n[P{i}] Relevance: {chunk.relevance_score:.0%}")
                sections.append(chunk.content)

        if blog_chunks:
            sections.append("\n### Engineering Blog Context")
            for i, chunk in enumerate(blog_chunks, 1):
                source = chunk.metadata.get("source", "Unknown")
                title = chunk.metadata.get("title", "")[:60]
                pub = chunk.metadata.get("published", "")
                sections.append(
                    f"\n[B{i}] {source} — '{title}' | {pub} | Relevance: {chunk.relevance_score:.0%}"
                )
                sections.append(chunk.content)

        return "\n".join(sections)

    def status(self) -> dict:
        return {
            "policies_collection": self.config["chromadb"]["policies_collection"],
            "policies_chunks": self.policies_collection.count() if self.policies_collection else 0,
            "blogs_collection": self.config["chromadb"]["blogs_collection"],
            "blogs_chunks": self.blogs_collection.count() if self.blogs_collection else 0,
            "blogs_ready": self.blogs_collection is not None
        }
