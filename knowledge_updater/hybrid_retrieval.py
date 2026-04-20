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
import re
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
                timeout=60
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
        # Phase 5A: new collections
        self.fixes_collection = self._get_collection(
            "devops_fixes_v1",
            required=False
        )
        self.patterns_collection = self._get_collection(
            "devops_patterns_v1",
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

    def _detect_query_intent(self, query_text: str) -> str:
        q = query_text.lower().strip()
        if any(k in q for k in ["architecture", "request flow", "data flow", "components", "topology", "diagram"]):
            return "architecture"
        if any(q.startswith(prefix) for prefix in ["what is", "what are", "define", "explain "]):
            return "definition"
        if any(k in q for k in ["compare", "difference between", "vs ", "versus"]):
            return "comparison"
        if any(k in q for k in ["error", "failed", "not working", "broken", "troubleshoot", "debug", "fix"]):
            return "troubleshooting"
        return "general"

    def _extract_core_terms(self, query_text: str) -> list:
        q = query_text.lower().strip()
        for prefix in ["what is ", "what are ", "define ", "explain "]:
            if q.startswith(prefix):
                q = q[len(prefix):]
                break
        q = re.sub(r"[^\w\s\-]", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        stop_words = {
            "a", "an", "the", "is", "are", "for", "of", "to", "in", "on", "and",
            "or", "with", "me", "about", "please"
        }
        return [term for term in q.split() if term and term not in stop_words]

    def _definition_bonus(self, chunk: RetrievedChunk, query_text: str, core_terms: list) -> int:
        if not core_terms:
            return 0

        text = chunk.content.lower()
        early_text = text[:200]
        title = (chunk.metadata.get("title", "") or "").lower()
        source = (chunk.metadata.get("source", "") or "").lower()
        phrase = " ".join(core_terms)
        bonus = 0

        if phrase and phrase in text:
            bonus += 18
        if phrase and phrase in early_text:
            bonus += 16
        if phrase and phrase in title:
            bonus += 10
        if any(term in source for term in core_terms):
            bonus += 4

        matched_terms = sum(1 for term in core_terms if term in text)
        bonus += matched_terms * 3

        definition_patterns = [
            f"{phrase} is",
            f"{phrase} are",
            "used to",
            "determines whether",
            "is a diagnostic",
            "is used to",
        ]
        if any(pattern.strip() and pattern in text for pattern in definition_patterns):
            bonus += 12

        return bonus

    def _architecture_bonus(self, chunk: RetrievedChunk, core_terms: list) -> int:
        if not core_terms:
            return 0
        text = chunk.content.lower()
        title = (chunk.metadata.get("title", "") or "").lower()
        source = (chunk.metadata.get("source", "") or "").lower()
        relation_terms = {
            "routes", "route", "gateway", "request", "data flow", "stream", "streams",
            "event", "events", "cache", "caches", "store", "stores", "writes",
            "reads", "process", "processes", "monitor", "monitors", "pipeline",
        }
        bonus = 0
        matched_terms = sum(1 for term in core_terms if term in text)
        bonus += matched_terms * 6
        if any(term in title for term in core_terms):
            bonus += 10
        if any(term in source for term in core_terms):
            bonus += 3
        relation_hits = sum(1 for term in relation_terms if term in text)
        bonus += min(relation_hits, 6) * 4
        if chunk.source_collection == "patterns":
            bonus += 18
        elif chunk.source_collection == "blogs":
            bonus += 6
        if matched_terms >= 2 and relation_hits >= 1:
            bonus += 20
        return bonus

    def _source_priority_bonus(self, query_intent: str, source_collection: str) -> int:
        """Intent-aware source weighting.

        Policies/patterns/fixes/blogs are not equally authoritative for every
        question type. This adjusts distance before final relevance sorting so
        blogs can support answers without becoming the primary source of truth.
        """
        priorities = {
            "definition": {
                "policies": 36,
                "patterns": 18,
                "fixes": 8,
                "blogs": -12,
            },
            "troubleshooting": {
                "fixes": 40,
                "policies": 28,
                "patterns": 10,
                "blogs": -16,
            },
            "architecture": {
                "patterns": 42,
                "policies": 18,
                "fixes": 10,
                "blogs": -6,
            },
            "comparison": {
                "policies": 30,
                "patterns": 16,
                "fixes": 8,
                "blogs": -10,
            },
        }
        return priorities.get(query_intent, {}).get(source_collection, 0)

    def _is_ava_scoped_query(self, query_text: str) -> bool:
        q = query_text.lower()
        ava_markers = (
            "ava", "your architecture", "your docker", "your containers",
            "your ports", "your stack", "your models", "ava-agent",
        )
        return any(marker in q for marker in ava_markers)

    def _is_ava_internal_chunk(self, chunk: RetrievedChunk) -> bool:
        text = chunk.content.lower()
        title = (chunk.metadata.get("title", "") or "").lower()
        source = (chunk.metadata.get("source", "") or "").lower()
        internal_markers = (
            "ava request pipeline",
            "ava-agent",
            "flask/gunicorn",
            "ollama host",
            "phase5a_ingestor.py",
            "devops-agent",
            "knowledge_updater",
            "port 5443",
            "postgresql 15",
            "hashicorp vault",
            "open policy agent",
        )
        return any(marker in text or marker in title or marker in source for marker in internal_markers)

    def _filter_architecture_chunks_for_query(self, chunks: list, query_text: str) -> list:
        if self._is_ava_scoped_query(query_text):
            return chunks
        filtered = [chunk for chunk in chunks if not self._is_ava_internal_chunk(chunk)]
        return filtered or chunks

    def query(
        self,
        query_text: str,
        n_policies: int = 4,
        n_blogs: int = 3,
        blog_min_relevance: float = 0.45,
        format_for_llm: bool = True
    ):
        start = time.time()
        query_intent = self._detect_query_intent(query_text)
        core_terms = self._extract_core_terms(query_text)

        embedding = self.embedder.embed(query_text)
        if not embedding:
            return "" if format_for_llm else []

        policy_chunks = self._query_collection(
            self.policies_collection, embedding, n_policies, "policies"
        )
        blog_fetch_count = max(n_blogs, 20)
        if query_intent == "definition":
            blog_fetch_count = max(n_blogs, 8)
        elif query_intent == "architecture":
            blog_fetch_count = max(n_blogs, 12)
        blog_chunks = self._query_collection(
            self.blogs_collection, embedding, blog_fetch_count, "blogs"
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

        # Phase 5A: query new collections based on intent
        fixes_chunks = []
        patterns_chunks = []
        if query_intent == "troubleshooting":
            fixes_chunks = self._query_collection(
                self.fixes_collection, embedding, 4, "fixes"
            )
            patterns_chunks = self._query_collection(
                self.patterns_collection, embedding, 2, "patterns"
            )
        elif query_intent == "architecture":
            patterns_chunks = self._query_collection(
                self.patterns_collection, embedding, 8, "patterns"
            )
            architecture_seed_terms = core_terms[:8]
            if architecture_seed_terms:
                pattern_seeded = self._keyword_fetch(self.patterns_collection, architecture_seed_terms, limit=8)
                blog_seeded = self._keyword_fetch(self.blogs_collection, architecture_seed_terms, limit=8)
                seen = set()
                merged_patterns = []
                for chunk in pattern_seeded + patterns_chunks:
                    key = chunk.content
                    if key in seen:
                        continue
                    merged_patterns.append(chunk)
                    seen.add(key)
                patterns_chunks = merged_patterns
                seen = set()
                merged_blogs = []
                for chunk in blog_seeded + blog_chunks:
                    key = chunk.content
                    if key in seen:
                        continue
                    merged_blogs.append(chunk)
                    seen.add(key)
                blog_chunks = merged_blogs
            patterns_chunks = self._filter_architecture_chunks_for_query(patterns_chunks, query_text)
            blog_chunks = self._filter_architecture_chunks_for_query(blog_chunks, query_text)
        else:
            patterns_chunks = self._query_collection(
                self.patterns_collection, embedding, 4, "patterns"
            )

        all_chunks = policy_chunks + blog_chunks
        if query_intent == "definition":
            for chunk in all_chunks:
                bonus = self._definition_bonus(chunk, query_text, core_terms)
                if bonus:
                    chunk.distance = max(0, chunk.distance - bonus)
        elif query_intent == "architecture":
            for chunk in policy_chunks + blog_chunks + patterns_chunks:
                bonus = self._architecture_bonus(chunk, core_terms)
                if bonus:
                    chunk.distance = max(0, chunk.distance - bonus)

        for chunk in policy_chunks + blog_chunks + fixes_chunks + patterns_chunks:
            source_bonus = self._source_priority_bonus(query_intent, chunk.source_collection)
            if source_bonus > 0:
                chunk.distance = max(0, chunk.distance - source_bonus)
            elif source_bonus < 0:
                chunk.distance += abs(source_bonus)

        for chunk in blog_chunks:
            keyword_hits = sum(1 for w in query_words if w in chunk.content.lower())
            # Extra boost for high-value technical terms
            bonus = sum(3 for term in BOOST_TERMS if term.lower() in chunk.content.lower())
            chunk.distance = max(0, chunk.distance - (keyword_hits * 5) - bonus)

        if query_intent == "definition":
            policy_chunks.sort(key=lambda x: x.relevance_score, reverse=True)
            blog_chunks.sort(key=lambda x: x.relevance_score, reverse=True)
        elif query_intent == "architecture":
            patterns_chunks.sort(key=lambda x: x.relevance_score, reverse=True)
            blog_chunks.sort(key=lambda x: x.relevance_score, reverse=True)
            policy_chunks.sort(key=lambda x: x.relevance_score, reverse=True)

        blog_chunks_filtered = [
            c for c in blog_chunks if c.relevance_score >= blog_min_relevance
        ]
        blog_chunks_filtered.sort(key=lambda x: x.relevance_score, reverse=True)

        elapsed = round((time.time() - start) * 1000, 1)
        logger.info(
            f"Hybrid retrieval: {len(policy_chunks)} policy + "
            f"{len(blog_chunks_filtered)} blog + "
            f"{len(fixes_chunks)} fixes + "
            f"{len(patterns_chunks)} patterns chunks in {elapsed}ms"
        )

        if not format_for_llm:
            if query_intent == "architecture":
                return patterns_chunks + blog_chunks_filtered + policy_chunks + fixes_chunks
            return policy_chunks + blog_chunks_filtered + fixes_chunks + patterns_chunks

        return self._format_context(
            policy_chunks,
            blog_chunks_filtered,
            fixes_chunks,
            patterns_chunks,
            query_intent=query_intent,
        )

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
            grouped[url]["parts"].append(self._strip_section_labels(chunk.content))

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

    _SECTION_LABEL_RE = re.compile(
        r'^(SUMMARY|REQUEST FLOW|ARCHITECTURE NOTES):\s*',
        re.MULTILINE | re.IGNORECASE,
    )

    def _strip_section_labels(self, text: str) -> str:
        """Remove structural doc labels (SUMMARY:, REQUEST FLOW:, ARCHITECTURE NOTES:)
        from chunk content so they don't bleed into LLM answers."""
        return self._SECTION_LABEL_RE.sub('', text)

    def _format_context(
        self,
        policy_chunks: list,
        blog_chunks: list,
        fixes_chunks: list = None,
        patterns_chunks: list = None,
        query_intent: str = "general",
    ) -> str:
        sections = []

        if query_intent == "architecture" and patterns_chunks:
            sections.append("### Infrastructure Patterns")
            for i, chunk in enumerate(patterns_chunks, 1):
                sections.append(f"\n[PT{i}] Relevance: {chunk.relevance_score:.0%}")
                sections.append(self._strip_section_labels(chunk.content))

        if policy_chunks:
            sections.append("### Trusted Knowledge Base")
            for i, chunk in enumerate(policy_chunks, 1):
                sections.append(f"\n[P{i}] Relevance: {chunk.relevance_score:.0%}")
                sections.append(self._strip_section_labels(chunk.content))

        if blog_chunks:
            sections.append("\n### Engineering Blog Context")
            for i, chunk in enumerate(blog_chunks, 1):
                source = chunk.metadata.get("source", "Unknown")
                title = chunk.metadata.get("title", "")[:60]
                pub = chunk.metadata.get("published", "")
                sections.append(
                    f"\n[B{i}] {source} — '{title}' | {pub} | Relevance: {chunk.relevance_score:.0%}"
                )
                sections.append(self._strip_section_labels(chunk.content))

        if fixes_chunks:
            sections.append("\n### Known Fixes & Solutions")
            for i, chunk in enumerate(fixes_chunks, 1):
                sections.append(f"\n[F{i}] Relevance: {chunk.relevance_score:.0%}")
                sections.append(self._strip_section_labels(chunk.content))

        if patterns_chunks and query_intent != "architecture":
            sections.append("\n### Infrastructure Patterns")
            for i, chunk in enumerate(patterns_chunks, 1):
                sections.append(f"\n[PT{i}] Relevance: {chunk.relevance_score:.0%}")
                sections.append(self._strip_section_labels(chunk.content))

        return "\n".join(sections)

    def status(self) -> dict:
        return {
            "policies_collection": self.config["chromadb"]["policies_collection"],
            "policies_chunks": self.policies_collection.count() if self.policies_collection else 0,
            "blogs_collection": self.config["chromadb"]["blogs_collection"],
            "blogs_chunks": self.blogs_collection.count() if self.blogs_collection else 0,
            "blogs_ready": self.blogs_collection is not None,
            "fixes_collection": "devops_fixes_v1",
            "fixes_chunks": self.fixes_collection.count() if self.fixes_collection else 0,
            "fixes_ready": self.fixes_collection is not None,
            "patterns_collection": "devops_patterns_v1",
            "patterns_chunks": self.patterns_collection.count() if self.patterns_collection else 0,
            "patterns_ready": self.patterns_collection is not None,
        }
