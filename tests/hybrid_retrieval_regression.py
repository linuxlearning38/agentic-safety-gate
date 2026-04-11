import ast
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SOURCE = Path(__file__).resolve().parents[1] / "knowledge_updater" / "hybrid_retrieval.py"


def load_retriever():
    src = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(SOURCE))
    namespace = {
        "dataclass": dataclass,
        "field": field,
        "Optional": Optional,
        "logging": logging,
        "logger": logging.getLogger("hybrid_retrieval_test"),
        "re": re,
        "time": time,
        "RetrievedChunk": None,
    }
    segments = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "HybridRetriever":
            segments.append(ast.get_source_segment(src, node))
    exec("\n\n".join(segments), namespace)
    return namespace["HybridRetriever"]


class DummyChunk:
    def __init__(self, content, source_collection, distance, metadata=None):
        self.content = content
        self.source_collection = source_collection
        self.distance = distance
        self.metadata = metadata or {}

    @property
    def relevance_score(self):
        return round(1.0 / (1.0 + self.distance), 4)

    @property
    def source_label(self):
        return self.metadata.get("source", self.source_collection)


class RetrievedChunk(DummyChunk):
    pass


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f"[PASS] {name}")


def main():
    HybridRetriever = load_retriever()
    retriever = HybridRetriever.__new__(HybridRetriever)

    check(
        "architecture intent detected",
        retriever._detect_query_intent("Explain Netflix architecture with Zuul and Kafka") == "architecture",
    )

    patterns_chunk = DummyChunk(
        content="Zuul routes requests to services while Kafka streams events to Cassandra and EVCache caches hot reads.",
        source_collection="patterns",
        distance=20.0,
        metadata={"title": "Netflix event-driven architecture"},
    )
    blog_chunk = DummyChunk(
        content="Kafka lag investigations improved over time.",
        source_collection="blogs",
        distance=20.0,
        metadata={"title": "Kafka SRE notes"},
    )
    pattern_bonus = retriever._architecture_bonus(
        patterns_chunk,
        ["zuul", "kafka", "cassandra", "evcache"],
    )
    blog_bonus = retriever._architecture_bonus(
        blog_chunk,
        ["zuul", "kafka", "cassandra", "evcache"],
    )
    check("architecture bonus prefers relationship-rich pattern chunks", pattern_bonus > blog_bonus)
    ava_internal_chunk = DummyChunk(
        content="PATTERN: AVA Request Pipeline\nDESCRIPTION: How a query flows through AVA from HTTP request to response",
        source_collection="patterns",
        distance=5.0,
        metadata={"title": "AVA Request Pipeline"},
    )
    filtered_external = retriever._filter_architecture_chunks_for_query(
        [ava_internal_chunk, patterns_chunk],
        "Explain Netflix architecture with Zuul and Kafka",
    )
    check("external architecture filter drops ava internal chunks", len(filtered_external) == 1 and filtered_external[0].content == patterns_chunk.content)
    filtered_self = retriever._filter_architecture_chunks_for_query(
        [ava_internal_chunk, patterns_chunk],
        "Create a mermaid diagram of your Docker architecture",
    )
    check("ava self architecture keeps internal chunks", len(filtered_self) == 2)

    policy_chunks = [DummyChunk("generic policy", "policies", 5.0, {})]
    blog_chunks = [blog_chunk]
    fixes_chunks = []
    patterns_chunks = [patterns_chunk]
    formatted = retriever._format_context(
        policy_chunks,
        blog_chunks,
        fixes_chunks,
        patterns_chunks,
        query_intent="architecture",
    )
    check("architecture context starts with patterns section", formatted.startswith("### Infrastructure Patterns"))
    assembled = retriever.assemble_context(
        [
            DummyChunk(
                content="SUMMARY: Kafka is the event backbone.",
                source_collection="blogs",
                distance=1.0,
                metadata={"url": "https://example.com/kafka", "source": "example", "title": "Kafka"},
            ),
            DummyChunk(
                content="REQUEST FLOW: Zuul routes requests to services.",
                source_collection="blogs",
                distance=1.1,
                metadata={"url": "https://example.com/kafka", "source": "example", "title": "Kafka"},
            ),
        ],
        max_articles=1,
        max_chunks_per_article=2,
    )
    check("assemble_context strips section labels", "SUMMARY:" not in assembled[0] and "REQUEST FLOW:" not in assembled[0])

    print("\nHybrid retrieval regression tests passed.")


if __name__ == "__main__":
    main()
