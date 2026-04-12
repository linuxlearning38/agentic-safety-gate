import ast
import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web_agent_v2.1_guardrail.py"


FUNCTIONS = {
    "_normalize_text",
    "_normalize_user_query",
    "_route_query",
    "_resolve_ava_self_response",
    "_resolve_memory_store_response",
    "_resolve_memory_recall_response",
    "_retrieve_troubleshooting_chunks",
    "_resolve_troubleshooting_response",
    "_extract_query_entities",
    "_normalize_fact_key",
    "_canonical_fact_label",
    "_fact_aliases",
    "_load_chat_facts",
    "_save_chat_fact",
    "_parse_memory_statement",
    "_extract_memory_request",
    "_extract_recall_label",
    "_recall_chat_fact",
    "_answer_ava_self_query",
    "detect_query_intent",
}

CONSTANTS = {
    "_MEMORY_FACT_KEY",
    "_ENTITY_STOP_WORDS",
    "_INFRA_COMPONENTS",
    "_KNOWN_DIAGRAM_TECH",
    "_GENERIC_HALLUCINATION_TERMS",
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def load_helpers():
    input_router = load_module("ava_input_router_for_helpers", ROOT / "control" / "input_router.py")
    evidence_selector = load_module("ava_evidence_selector_for_helpers", ROOT / "control" / "evidence_selector.py")
    answer_planner = load_module("ava_answer_planner_for_helpers", ROOT / "control" / "answer_planner.py")
    response_composer = load_module("ava_response_composer_for_helpers", ROOT / "control" / "response_composer.py")
    src = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(SOURCE))
    namespace = {
        "json": json,
        "re": re,
        "datetime": datetime,
        "route_query": input_router.route_query,
        "select_ava_self_evidence": evidence_selector.select_ava_self_evidence,
        "select_memory_store_evidence": evidence_selector.select_memory_store_evidence,
        "select_memory_recall_evidence": evidence_selector.select_memory_recall_evidence,
        "select_troubleshooting_evidence": evidence_selector.select_troubleshooting_evidence,
        "format_ava_self_facts_block": evidence_selector.format_ava_self_facts_block,
        "build_ava_self_plan": answer_planner.build_ava_self_plan,
        "build_memory_store_plan": answer_planner.build_memory_store_plan,
        "build_memory_recall_plan": answer_planner.build_memory_recall_plan,
        "build_troubleshooting_plan": answer_planner.build_troubleshooting_plan,
        "compose_controlled_response": response_composer.compose_response,
        "db": type("FakeDB", (), {
            "__init__": lambda self: setattr(self, "memory", {}),
            "get_memory": lambda self, key, default=None: self.memory.get(key, default),
            "save_memory": lambda self, key, value: self.memory.__setitem__(key, value),
        })(),
    }
    class FakeChunk:
        def __init__(self, content, source_collection):
            self.content = content
            self.source_collection = source_collection
    class FakeHybridRetriever:
        def query(self, query_text, n_policies=4, n_blogs=3, blog_min_relevance=0.45, format_for_llm=True):
            q = query_text.lower()
            chunks = []
            if "oomkilled" in q or "oom killed" in q:
                chunks.append(FakeChunk("OOMKilled happens when the container exceeds its memory limit.", "policies"))
                chunks.append(FakeChunk("Reduce memory usage or raise limits after checking actual peaks.", "fixes"))
            if "crashloopbackoff" in q or "crashloop" in q:
                chunks.append(FakeChunk("CrashLoopBackOff means repeated start-fail-restart cycles.", "policies"))
                chunks.append(FakeChunk("Check logs, env vars, entrypoint, and probe settings.", "fixes"))
            if "service is down" in q or "service down" in q:
                chunks.append(FakeChunk("Check endpoints, readiness, ingress, and DNS when a service is down.", "fixes"))
            chunks.append(FakeChunk("blog noise", "blogs"))
            return chunks
        def _strip_section_labels(self, text):
            return text
    namespace["hybrid_retriever"] = FakeHybridRetriever()
    segments = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(name in CONSTANTS for name in targets):
                segments.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            segments.append(ast.get_source_segment(src, node))
    exec("\n\n".join(segments), namespace)
    return namespace


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f"[PASS] {name}")


def main():
    ns = load_helpers()
    router = load_module("ava_input_router", ROOT / "control" / "input_router.py")

    benchmark_queries = [
        ("What models are you running?", "models", "qwen2.5:14b"),
        ("1. What models are you running?", "models", "qwen2.5:14b"),
        ("2. 1. What models are you running?", "models", "qwen2.5:14b"),
        ("1.2.4.a.b.c.d.e.---a-b-44= what models are you running?", "models", "qwen2.5:14b"),
        ("What is your knowledge base size?", "knowledge_base", "chunks across"),
        ("What Docker containers and ports are you running?", "containers", "ava-agent"),
    ]

    about = {
        "version": "2.1.2",
        "built_by": "Manoj, Delhi",
        "runtime": "WSL2 Ubuntu, RTX 5060 Ti 16GB, Ryzen 1600, 32GB RAM",
        "containers": {
            "ava-agent": {"port": 5443, "proto": "HTTPS", "stack": "Flask/Gunicorn, 2 workers"},
            "agent_postgres": {"port": 5432, "stack": "PostgreSQL 15"},
        },
        "models": {
            "llm": "qwen2.5:14b (Q4_K_M quantization)",
            "embedding": "nomic-embed-text",
            "vision": "llava:13b",
            "ollama_host": "http://host.docker.internal:11434",
        },
        "knowledge_base": {
            "devops_policies_v2": 3885,
            "devops_blogs_v1": 2513,
            "devops_fixes_v1": 20,
            "devops_patterns_v1": 64,
        },
    }

    for query, topic, expected in benchmark_queries:
        route = router.route_query(
            query,
            normalizer=ns["_normalize_user_query"],
            entity_extractor=ns["_extract_query_entities"],
        )
        check(f"route {query!r} to ava_self", route.intent == "ava_self")
        check(f"topic {query!r}", route.topic == topic)
        check(f"detect_query_intent {query!r}", ns["detect_query_intent"](query) == "ava_self")
        answer = ns["_answer_ava_self_query"](query, about=about)
        check(f"answer {query!r}", expected.lower() in answer.lower())

    memory_store_queries = [
        "Remember this: server=prod-india-01",
        "2. Remember this: server=prod-india-01",
        "- Remember this: cluster=prod-west-2",
    ]
    for query in memory_store_queries:
        route = ns["_route_query"](query)
        check(f"memory route {query!r}", route.intent == "memory_store")
        stored = ns["_resolve_memory_store_response"](query)
        check(f"memory store response {query!r}", "remember" in stored["response"].lower() or "your" in stored["response"].lower())

    memory_recall_queries = [
        ("What is my server name?", "prod-india-01"),
        ("What's my server name?", "prod-india-01"),
        ("what is my cluster name?", "prod-west-2"),
    ]
    for query, expected in memory_recall_queries:
        route = ns["_route_query"](query)
        check(f"memory recall route {query!r}", route.intent == "memory_recall")
        recalled = ns["_resolve_memory_recall_response"](query)
        check(f"memory recall response {query!r}", expected.lower() in recalled["response"].lower())

    troubleshooting_queries = [
        ("What causes OOMKilled in Kubernetes?", "oomkilled", "memory limit"),
        ("My nginx pod is CrashLoopBackOff", "crashloopbackoff", "CrashLoopBackOff"),
        ("My service is down", "service_down", "service being down usually means"),
    ]
    for query, topic, expected in troubleshooting_queries:
        route = ns["_route_query"](query)
        check(f"troubleshooting route {query!r}", route.intent == "troubleshooting")
        check(f"troubleshooting topic {query!r}", route.topic == topic)
        check(f"detect_query_intent {query!r}", ns["detect_query_intent"](query) == "troubleshooting")
        resolved = ns["_resolve_troubleshooting_response"](query)
        check(f"troubleshooting response {query!r}", expected.lower() in resolved["response"].lower())
        check(f"troubleshooting sources filtered {query!r}", resolved["sources_used"] >= 1)

    print("\nAVA controlled benchmark passed.")


if __name__ == "__main__":
    main()
