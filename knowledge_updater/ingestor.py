"""
AVA Knowledge Updater - Phase 2
ingestor.py — ChromaDB ingestion with dedup (URL + content hash) and chunking

Writes to devops_blogs_v1 ONLY.
Never touches devops_policies_v2.
"""

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import chromadb
import requests
from chromadb.config import Settings

from .scraper import Article

logger = logging.getLogger("ava.ingestor")


class TextChunker:
    def __init__(self, chunk_size: int, chunk_overlap: int, min_chunk_size: int):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_size = min_chunk_size

    def chunk(self, text: str) -> list:
        text = re.sub(r'\n{3,}', '\n\n', text).strip()

        if len(text) <= self.chunk_size:
            if len(text) >= self.min_chunk_size:
                return [text]
            return []

        chunks = []
        start = 0

        while start < len(text):
            end = start + self.chunk_size

            if end >= len(text):
                chunk = text[start:].strip()
                if len(chunk) >= self.min_chunk_size:
                    chunks.append(chunk)
                break

            break_pos = text.rfind('\n\n', start, end)
            if break_pos == -1 or break_pos <= start:
                break_pos = text.rfind('. ', start, end)
            if break_pos == -1 or break_pos <= start:
                break_pos = text.rfind(' ', start, end)
            if break_pos == -1 or break_pos <= start:
                break_pos = end

            chunk = text[start:break_pos].strip()
            if len(chunk) >= self.min_chunk_size:
                chunks.append(chunk)

            start = max(break_pos - self.chunk_overlap, start + 1)

        return chunks


class EmbeddingClient:
    def __init__(self, ollama_url: str, model: str):
        self.url = f"{ollama_url}/api/embeddings"
        self.model = model

    def embed(self, text: str) -> Optional[list]:
        try:
            resp = requests.post(
                self.url,
                json={"model": self.model, "prompt": text},
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except Exception as e:
            logger.error(f"Embedding error: {e}")
            return None


class DeduplicationChecker:
    def __init__(self, collection, config: dict):
        self.collection = collection
        self.config = config

    def is_url_indexed(self, url_hash: str) -> bool:
        if not self.config["check_url"]:
            return False
        try:
            results = self.collection.get(
                where={"url_hash": url_hash},
                limit=1,
                include=["metadatas"]
            )
            exists = len(results["ids"]) > 0
            if exists:
                logger.info(f"URL already indexed (hash: {url_hash[:12]}...)")
            return exists
        except Exception as e:
            logger.warning(f"URL dedup check failed: {e}")
            return False

    def is_chunk_indexed(self, chunk_hash: str) -> bool:
        if not self.config["check_content_hash"]:
            return False
        try:
            results = self.collection.get(
                where={"chunk_hash": chunk_hash},
                limit=1,
                include=["metadatas"]
            )
            return len(results["ids"]) > 0
        except Exception as e:
            logger.warning(f"Chunk dedup check failed: {e}")
            return False


class KnowledgeIngestor:
    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.chunker = TextChunker(
            chunk_size=config["chunking"]["chunk_size"],
            chunk_overlap=config["chunking"]["chunk_overlap"],
            min_chunk_size=config["chunking"]["min_chunk_size"]
        )
        self.embedder = EmbeddingClient(
            ollama_url=config["embedding"]["ollama_url"],
            model=config["embedding"]["model"]
        )
        self.client = None
        self.collection = None
        self.dedup = None

        if not dry_run:
            self._init_chromadb()

    def _init_chromadb(self):
        try:
            self.client = chromadb.PersistentClient(
                path=self.config["chromadb"]["persist_directory"],
                settings=Settings(anonymized_telemetry=False)
            )
            collection_name = self.config["chromadb"]["blogs_collection"]
            self.collection = self.client.get_or_create_collection(
                name=collection_name,
                metadata={
                    "description": "AVA dynamic knowledge — engineering blog articles",
                    "created_by": "knowledge_updater/ingestor.py",
                    "phase": "2"
                }
            )
            self.dedup = DeduplicationChecker(self.collection, self.config["dedup"])
            logger.info(f"ChromaDB connected. Collection '{collection_name}' ready.")
            existing = self.collection.count()
            logger.info(f"Existing chunks in collection: {existing}")
        except Exception as e:
            logger.error(f"ChromaDB init failed: {e}")
            raise

    def _make_chunk_id(self, url_hash: str, chunk_index: int) -> str:
        return f"blog_{url_hash[:16]}_{chunk_index:04d}"

    def ingest_article(self, article: Article) -> dict:
        stats = {
            "url": article.url,
            "title": article.title,
            "source": article.source_name,
            "quality_score": article.quality_score,
            "chunks_added": 0,
            "chunks_skipped": 0,
            "skipped": False,
            "reason": None
        }

        if not self.dry_run and self.dedup.is_url_indexed(article.url_hash):
            stats["skipped"] = True
            stats["reason"] = "url_already_indexed"
            return stats

        chunks = self.chunker.chunk(article.content)
        if not chunks:
            stats["skipped"] = True
            stats["reason"] = "no_valid_chunks"
            return stats

        # Inject a summary chunk at the start — improves broad query matching
        summary_text = (
            f"Article: {article.title}\n"
            f"Source: {article.source_name}\n"
            f"Published: {article.published_date}\n"
            f"URL: {article.url}\n\n"
            f"{article.content[:600]}"
        )
        chunks = [summary_text] + chunks

        logger.info(f"Processing '{article.title[:50]}' -> {len(chunks)} chunks (inc. summary)")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would ingest {len(chunks)} chunks from: {article.title[:60]}")
            stats["chunks_added"] = len(chunks)
            return stats

        ids_to_add = []
        embeddings_to_add = []
        documents_to_add = []
        metadatas_to_add = []

        for i, chunk_text in enumerate(chunks):
            # Prepend article title for better semantic matching
            chunk_text = f"Article: {article.title}\n\n{chunk_text}"
            chunk_hash = hashlib.sha256(chunk_text.encode()).hexdigest()

            if self.dedup.is_chunk_indexed(chunk_hash):
                stats["chunks_skipped"] += 1
                continue

            embedding = self.embedder.embed(chunk_text)
            if not embedding:
                logger.warning(f"Failed to embed chunk {i} of {article.title[:40]}")
                stats["chunks_skipped"] += 1
                continue

            chunk_id = self._make_chunk_id(article.url_hash, i)
            chunk_metadata = {
                **article.metadata,
                "chunk_index": i,
                "chunk_total": len(chunks),
                "chunk_hash": chunk_hash,
                "url_hash": article.url_hash,
                "quality_score": str(article.quality_score),
                "ingested_at": datetime.now(timezone.utc).isoformat()
            }

            ids_to_add.append(chunk_id)
            embeddings_to_add.append(embedding)
            documents_to_add.append(chunk_text)
            metadatas_to_add.append(chunk_metadata)

        if ids_to_add:
            try:
                self.collection.upsert(
                    ids=ids_to_add,
                    embeddings=embeddings_to_add,
                    documents=documents_to_add,
                    metadatas=metadatas_to_add
                )
                stats["chunks_added"] = len(ids_to_add)
                logger.info(f"✓ Ingested {len(ids_to_add)} chunks from: {article.title[:60]}")
            except Exception as e:
                logger.error(f"ChromaDB upsert failed for {article.title}: {e}")
                stats["reason"] = f"chromadb_error: {e}"

        return stats

    def ingest_all(self, articles: list) -> dict:
        run_summary = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": self.dry_run,
            "total_articles": len(articles),
            "articles_ingested": 0,
            "articles_skipped": 0,
            "total_chunks_added": 0,
            "total_chunks_skipped": 0,
            "articles": []
        }

        for article in articles:
            try:
                stats = self.ingest_article(article)
                run_summary["articles"].append(stats)

                if stats["skipped"]:
                    run_summary["articles_skipped"] += 1
                    logger.info(f"Skipped: {article.title[:50]} ({stats['reason']})")
                else:
                    run_summary["articles_ingested"] += 1
                    run_summary["total_chunks_added"] += stats["chunks_added"]
                    run_summary["total_chunks_skipped"] += stats["chunks_skipped"]

            except Exception as e:
                logger.error(f"Failed to ingest '{article.title}': {e}", exc_info=True)

        if not self.dry_run and self.collection:
            run_summary["collection_total_chunks"] = self.collection.count()

        logger.info(f"{'='*50}")
        logger.info(f"Ingestion complete:")
        logger.info(f"  Articles ingested: {run_summary['articles_ingested']}")
        logger.info(f"  Articles skipped:  {run_summary['articles_skipped']}")
        logger.info(f"  Chunks added:      {run_summary['total_chunks_added']}")
        logger.info(f"  Chunks skipped:    {run_summary['total_chunks_skipped']}")
        if not self.dry_run and self.collection:
            logger.info(f"  Collection total:  {run_summary['collection_total_chunks']}")

        return run_summary
