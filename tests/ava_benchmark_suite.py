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
    "_retrieve_architecture_chunks",
    "_resolve_architecture_response",
    "_retrieve_comparison_chunks",
    "_resolve_follow_up_response",
    "_resolve_comparison_response",
    "_retrieve_troubleshooting_chunks",
    "_resolve_troubleshooting_response",
    "_get_recent_distinct_turns",
    "_summarize_topic",
    "_topic_from_turn",
    "_topic_signature",
    "_response_summary",
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
        "select_architecture_evidence": evidence_selector.select_architecture_evidence,
        "select_comparison_evidence": evidence_selector.select_comparison_evidence,
        "select_follow_up_evidence": evidence_selector.select_follow_up_evidence,
        "select_memory_store_evidence": evidence_selector.select_memory_store_evidence,
        "select_memory_recall_evidence": evidence_selector.select_memory_recall_evidence,
        "select_troubleshooting_evidence": evidence_selector.select_troubleshooting_evidence,
        "format_ava_self_facts_block": evidence_selector.format_ava_self_facts_block,
        "build_ava_self_plan": answer_planner.build_ava_self_plan,
        "build_architecture_plan": answer_planner.build_architecture_plan,
        "build_comparison_plan": answer_planner.build_comparison_plan,
        "build_follow_up_plan": answer_planner.build_follow_up_plan,
        "build_memory_store_plan": answer_planner.build_memory_store_plan,
        "build_memory_recall_plan": answer_planner.build_memory_recall_plan,
        "build_troubleshooting_plan": answer_planner.build_troubleshooting_plan,
        "compose_controlled_response": response_composer.compose_response,
        "db": type("FakeDB", (), {
            "__init__": lambda self: setattr(self, "memory", {}),
            "get_memory": lambda self, key, default=None: self.memory.get(key, default),
            "save_memory": lambda self, key, value: self.memory.__setitem__(key, value),
            "get_recent_queries": lambda self, n=5: getattr(self, "queries", [])[-n:],
        })(),
        "_get_about_data": lambda: {
            "version": "2.1.2",
            "built_by": "Manoj, Delhi",
            "runtime": "WSL2 Ubuntu, RTX 5060 Ti 16GB, Ryzen 1600, 32GB RAM",
            "containers": {
                "ava-agent": {"port": 5443, "proto": "HTTPS", "stack": "Flask/Gunicorn, 2 workers"},
                "agent_postgres": {"port": 5432, "stack": "PostgreSQL 15"},
                "agent_redis": {"port": 6379, "stack": "Redis 7"},
                "agent_opa": {"port": 8181, "stack": "Open Policy Agent"},
                "agent_vault": {"port": 8200, "stack": "HashiCorp Vault"},
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
        },
    }
    class FakeChunk:
        def __init__(self, content, source_collection):
            self.content = content
            self.source_collection = source_collection
    class FakeHybridRetriever:
        def query(self, query_text, n_policies=4, n_blogs=3, blog_min_relevance=0.45, format_for_llm=True):
            q = query_text.lower()
            chunks = []
            if "netflix" in q or "zuul" in q or "evcache" in q:
                chunks.extend([
                    FakeChunk("SUMMARY: Zuul is the front door for requests from devices and web sites to backend Netflix services.", "patterns"),
                    FakeChunk("REQUEST FLOW: Client-facing services publish to Kafka after handling synchronous requests or internal state changes.", "patterns"),
                    FakeChunk("ARCHITECTURE NOTES: Kafka is the event transport and fan-out layer rather than the serving database.", "blogs"),
                    FakeChunk("Samza consumes Kafka streams and writes processed aggregates to Cassandra.", "patterns"),
                    FakeChunk("EVCache serves hot reads to reduce latency in front of Cassandra.", "patterns"),
                    FakeChunk("# Queue depth: kafka_consumer_lag, redis_blocked_clients", "blogs"),
                ])
            if "kubernetes deployment flow" in q:
                chunks.extend([
                    FakeChunk("Requests enter through an ingress or load balancer and are routed to a Kubernetes Service.", "patterns"),
                    FakeChunk("The Service sends traffic to healthy Pods, and Pods persist data through their configured backing stores.", "patterns"),
                ])
            if "blue-green" in q or "canary" in q:
                chunks.extend([
                    FakeChunk("Blue-green deployment keeps two production environments so traffic can switch all at once after validation.", "policies"),
                    FakeChunk("Canary deployment gradually shifts a small percentage of traffic to the new version before a full rollout.", "policies"),
                ])
            if "readiness probe" in q or "liveness probe" in q:
                chunks.extend([
                    FakeChunk("A readiness probe decides whether a container should receive traffic.", "policies"),
                    FakeChunk("A liveness probe decides whether Kubernetes should restart an unhealthy container.", "policies"),
                ])
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

    architecture_queries = [
        ("Explain Netflix architecture with Zuul, Kafka, Cassandra, EVCache", "external", "text", "**Components and Roles:**", "Zuul"),
        ("Create a mermaid diagram of your Docker architecture", "self_runtime", "diagram", "```mermaid", "ava-agent:5443"),
        ("Draw a diagram showing Kubernetes deployment flow", "external", "diagram", "```mermaid", "Ingress / Gateway"),
    ]
    for query, topic, response_mode, expected, extra in architecture_queries:
        route = ns["_route_query"](query)
        check(f"architecture route {query!r}", route.intent == "architecture")
        check(f"architecture topic {query!r}", route.topic == topic)
        check(f"architecture mode {query!r}", route.response_mode == response_mode)
        check(f"detect_query_intent {query!r}", ns["detect_query_intent"](query) == "architecture")
        resolved = ns["_resolve_architecture_response"](query)
        check(f"architecture response {query!r}", expected.lower() in resolved["response"].lower())
        check(f"architecture detail {query!r}", extra.lower() in resolved["response"].lower())

    ns["db"].queries = [
        {"query": "Explain blue-green vs canary deployment", "response": "**blue-green:** switch traffic all at once.\n**canary:** shift traffic gradually.", "intent": "comparison"},
        {"query": "What is a Kubernetes readiness probe?", "response": "A readiness probe decides whether a container should receive traffic.", "intent": "definition"},
    ]
    follow_up_queries = [
        "How is it different from the previous thing I asked?",
        "What did I just ask?",
        "Can you compare that with what we discussed?",
    ]
    for query in follow_up_queries:
        route = ns["_route_query"](query)
        check(f"follow_up route {query!r}", route.intent == "follow_up")
        check(f"detect_query_intent {query!r}", ns["detect_query_intent"](query) == "follow_up")
        resolved = ns["_resolve_follow_up_response"](query)
        check(f"follow_up response {query!r}", "readiness probe" in resolved["response"].lower() or "blue green" in resolved["response"].lower())

    comparison_queries = [
        ("Explain blue-green vs canary deployment", "blue-green", "canary"),
        ("What is the difference between readiness probe and liveness probe?", "readiness probe", "liveness probe"),
    ]
    for query, left, right in comparison_queries:
        route = ns["_route_query"](query)
        check(f"comparison route {query!r}", route.intent == "comparison")
        check(f"detect_query_intent {query!r}", ns["detect_query_intent"](query) == "comparison")
        resolved = ns["_resolve_comparison_response"](query)
        check(f"comparison response {query!r}", left.lower() in resolved["response"].lower() and right.lower() in resolved["response"].lower())

    print("\nAVA controlled benchmark passed.")


if __name__ == "__main__":
    main()
