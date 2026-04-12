import ast
import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "web_agent_v2.1_guardrail.py"

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
    "_normalize_fact_key",
    "_canonical_fact_label",
    "_fact_aliases",
    "_load_chat_facts",
    "_save_chat_fact",
    "_parse_memory_statement",
    "_extract_memory_request",
    "_extract_recall_label",
    "_recall_chat_fact",
    "_get_recent_distinct_turns",
    "_summarize_topic",
    "_topic_from_turn",
    "_topic_signature",
    "_response_summary",
    "_grounding_confident_enough",
    "_is_healing_query",
    "_json_only_requested",
    "_predict_heal_action",
    "_format_playbook_template",
    "_build_healing_response",
    "_extract_query_entities",
    "_diagram_entities_from_text",
    "_extract_relevant_context_lines",
    "_is_noisy_architecture_line",
    "_build_diagram_grounding_block",
    "_is_ava_self_architecture_query",
    "_looks_like_invalid_json_wrapper",
    "_repair_definition_wrapper",
    "_answer_ava_self_query",
    "detect_query_intent",
}

CONSTANTS = {
    "_MEMORY_FACT_KEY",
    "_ENTITY_STOP_WORDS",
    "_INFRA_COMPONENTS",
    "_KNOWN_DIAGRAM_TECH",
}


class FakeDB:
    def __init__(self):
        self.memory = {}
        self.queries = []

    def get_memory(self, key, default=None):
        return self.memory.get(key, default)

    def save_memory(self, key, value):
        self.memory[key] = value

    def get_recent_queries(self, n=5):
        return self.queries[-n:]


class FakeHealer:
    def detect_issue(self, source, message):
        msg = message.lower()
        if "crashloopbackoff" in msg:
            return {
                "issue_type": "pod_crash",
                "confidence": 0.8,
                "entities": {"name": "nginx", "pod_name": "nginx-deployment", "namespace": "default"},
            }
        return {
            "issue_type": "disk_full",
            "confidence": 0.9,
            "entities": {"name": "worker-1"},
        }

    def get_healing_action(self, issue_type):
        actions = {
            "pod_crash": {
                "command": "kubectl rollout restart deployment/{name}",
                "risk_level": "LOW",
                "rollback": "kubectl rollout undo deployment/{name}",
            },
            "disk_full": {
                "command": "find /var/log -name '*.log' -mtime +7 -delete",
                "risk_level": "LOW",
                "rollback": None,
            },
        }
        return actions.get(issue_type, {})


class FakeChunk:
    def __init__(self, content, source_collection="policies"):
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
            chunks.append(FakeChunk("Increase memory limits only after checking peak usage and leaks.", "fixes"))
        if "crashloopbackoff" in q or "crashloop" in q:
            chunks.append(FakeChunk("CrashLoopBackOff indicates repeated start-fail-restart cycles.", "policies"))
            chunks.append(FakeChunk("Check logs, probe settings, config mounts, and entrypoint failures.", "fixes"))
        if "service is down" in q or "service down" in q:
            chunks.append(FakeChunk("Check endpoints, readiness, ingress, and DNS for service-down incidents.", "fixes"))
        chunks.append(FakeChunk("blog noise that should be filtered", "blogs"))
        return chunks

    def _strip_section_labels(self, text):
        return text


def load_helpers():
    def load_module(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module

    src = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(SOURCE))
    input_router = load_module("ava_input_router", SOURCE.parent / "control" / "input_router.py")
    evidence_selector = load_module("ava_evidence_selector", SOURCE.parent / "control" / "evidence_selector.py")
    answer_planner = load_module("ava_answer_planner", SOURCE.parent / "control" / "answer_planner.py")
    response_composer = load_module("ava_response_composer", SOURCE.parent / "control" / "response_composer.py")
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
        "db": FakeDB(),
        "healer": FakeHealer(),
        "hybrid_retriever": FakeHybridRetriever(),
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
    fake_db = ns["db"]
    memory_key = ns["_MEMORY_FACT_KEY"]

    fake_db.memory[memory_key] = {
        "cluster": {"label": "cluster", "value": "prod-west-2", "updated_at": "now"}
    }

    check(
        "memory request parse",
        ns["_extract_memory_request"]("Remember this exactly: cluster=prod-west-2. What is my cluster name?")["fact"]["value"] == "prod-west-2",
    )
    check(
        "memory request parse plain remember",
        ns["_extract_memory_request"]("Remember: server=prod-india-01")["fact"]["label"] == "server",
    )
    check(
        "memory store route is controlled",
        ns["_route_query"]("2. Remember this: server=prod-india-01").intent == "memory_store",
    )
    cleaned_query = ns["_normalize_user_query"]("2. 1. what models are you running?")
    check("numbered query prefixes strip repeatedly", cleaned_query == "what models are you running?")
    noisy_query = ns["_normalize_user_query"]("1.2.4.a.b.c.d.e.---a-b-44-4-4-4-4-4-= what models are you running?")
    check("noisy query prefixes strip aggressively", noisy_query == "what models are you running?")
    check(
        "declarative troubleshooting query preserved during normalization",
        ns["_normalize_user_query"]("My service is down") == "My service is down",
    )
    saved_server = ns["_save_chat_fact"]("server", "prod-india-01")
    check("memory label canonicalized", saved_server["label"] == "server name")
    check(
        "memory recall alias",
        ns["_recall_chat_fact"]("cluster name")["value"] == "prod-west-2",
    )
    check(
        "memory recall server name alias",
        ns["_recall_chat_fact"]("server name")["value"] == "prod-india-01",
    )
    check(
        "recall label extract",
        ns["_extract_recall_label"]("What is my cluster name?") == "cluster name",
    )
    check(
        "recall label extract contractions",
        ns["_extract_recall_label"]("What's my server name?") == "server name",
    )
    check(
        "memory recall route is controlled",
        ns["_route_query"]("What is my server name?").intent == "memory_recall",
    )
    check(
        "troubleshooting route is controlled",
        ns["_route_query"]("What causes OOMKilled in Kubernetes?").intent == "troubleshooting",
    )
    architecture_route = ns["_route_query"]("Explain Netflix architecture with Zuul, Kafka, Cassandra, EVCache")
    check("architecture route is controlled", architecture_route.intent == "architecture")
    check("architecture route topic", architecture_route.topic == "external")
    check("architecture route mode", architecture_route.response_mode == "text")
    architecture_diagram_route = ns["_route_query"]("Create a mermaid diagram of your Docker architecture")
    check("architecture diagram route is controlled", architecture_diagram_route.intent == "architecture")
    check("architecture diagram topic", architecture_diagram_route.topic == "self_runtime")
    check("architecture diagram mode", architecture_diagram_route.response_mode == "diagram")
    check(
        "follow_up route is controlled",
        ns["_route_query"]("How is it different from the previous thing I asked?").intent == "follow_up",
    )
    comparison_route = ns["_route_query"]("What is the difference between readiness probe and liveness probe?")
    check("comparison route is controlled", comparison_route.intent == "comparison")
    check("comparison route extracts targets", len(comparison_route.comparison_targets) == 2)

    fake_db.queries = [
        {"query": "Remember this exactly: cluster=prod-west-2", "response": "Okay", "intent": "memory"},
        {"query": "What is my cluster name?", "response": "Your cluster is prod-west-2.", "intent": "memory"},
        {"query": "What is the difference between readiness probe and liveness probe?", "response": "Readiness probe checks if a container is ready. Liveness probe checks if it is healthy enough to keep running.", "intent": "definition"},
    ]
    check("response summary strips mermaid fence", ns["_response_summary"]("```mermaid\ngraph TD\nA-->B\n```\n\nGrounded explanation.") == "Grounded explanation.")
    check("response summary skips headings", ns["_response_summary"]("**Explanation:**\n- ava-agent uses Redis.") == "- ava-agent uses Redis.")
    check("response summary skips plain section label", ns["_response_summary"]("Explanation:\n- ava-agent uses Redis.") == "- ava-agent uses Redis.")
    check(
        "response summary skips generic diagram bullet",
        ns["_response_summary"](
            "```mermaid\ngraph LR\nA-->B\n```\n\nExplanation:\n- The diagram shows the components of the Docker architecture.\n- AVA runs as ava-agent on port 5443."
        ) == "- AVA runs as ava-agent on port 5443.",
    )
    check(
        "architecture grounding allows relation-rich medium context",
        ns["_grounding_confident_enough"](
            "Explain Netflix architecture with Zuul, Kafka, Cassandra, EVCache, Samza, Mantis",
            ["Zuul routes API requests to services.\nKafka carries events to Cassandra.\nEVCache caches hot reads."],
            "medium",
        ) is True,
    )

    fake_db.queries = [
        {"query": "Create a mermaid diagram of your Docker architecture.", "response": "```mermaid\ngraph LR\nA[ava-agent]-->B[Redis]\n```\n\nAVA uses Redis and Vault.", "intent": "architecture"},
        {"query": "How is it different from the previous thing I asked?", "response": "topic compare", "intent": "follow_up"},
        {"query": "What is the difference between readiness probe and liveness probe?", "response": "Readiness probe checks if a container is ready. Liveness probe checks if it is healthy enough to keep running.", "intent": "definition"},
    ]
    distinct_turns = ns["_get_recent_distinct_turns"](limit=4)
    check("distinct turns skip follow_up rows", len(distinct_turns) == 2)
    architecture_signature = ns["_topic_signature"](distinct_turns[0])
    check("topic signature prefers architecture entities", "ava agent" in architecture_signature and "postgresql" in architecture_signature)

    crash_response, crash_meta = ns["_build_healing_response"](
        "A pod nginx-deployment is in CrashLoopBackOff. Classify the issue type, confidence, command, risk level, rollback, and whether you would auto-execute or queue for approval."
    )
    check("healing route pod_crash", crash_meta["issue_type"] == "pod_crash")
    check("healing route queued_for_approval", crash_meta["action_taken"] == "queued_for_approval")
    check("healing route command templated", "kubectl rollout restart deployment/nginx" in crash_response)

    disk_json, disk_meta = ns["_build_healing_response"](
        'Disk usage on worker-1 is 95%. Respond only in this JSON format: {"issue_type":"","command":"","risk_level":"","rollback":"","action_taken":""}'
    )
    parsed_disk = json.loads(disk_json)
    check("json-only healing valid json", parsed_disk["issue_type"] == "disk_full")
    check("json-only healing low risk", parsed_disk["risk_level"] == "LOW")

    entities = ns["_diagram_entities_from_text"](
        "Netflix architecture with Zuul, Kafka, Cassandra, EVCache, Samza, Mantis, Netty"
    )
    check("diagram entities include kafka", "kafka" in [e.lower() for e in entities])
    check("diagram entities include zuul", "zuul" in [e.lower() for e in entities])

    grounding = ns["_build_diagram_grounding_block"](
        "Explain the Netflix diagram",
        ["Zuul handles API gateway traffic\nKafka carries events\nCassandra stores metadata"],
        entities,
    )
    check("diagram grounding mentions detected entities", "zuul" in grounding.lower())
    check("diagram grounding mentions kafka", "kafka" in grounding.lower())
    relevant_lines = ns["_extract_relevant_context_lines"](
        ["Zuul routes API requests to Kafka.\nKafka writes events to Cassandra.\nKafka writes events to Cassandra."],
        ["Zuul", "Kafka", "Cassandra"],
    )
    check("relevant context lines return matches", len(relevant_lines) == 2)
    check("relevant context lines dedupe duplicates", relevant_lines[0] != "" and len(set(relevant_lines)) == len(relevant_lines))
    check("architecture noise line detected", ns["_is_noisy_architecture_line"]("# Queue depth: kafka_consumer_lag, redis_blocked_clients") is True)

    check(
        "ava self architecture query detected",
        ns["_is_ava_self_architecture_query"](
            "Create a mermaid diagram of your Docker architecture.",
            ["ava-agent", "Redis"],
        ) is True,
    )
    check(
        "topic from ava mermaid turn stays readable",
        ns["_topic_from_turn"]({
            "query": "Create a mermaid diagram of your Docker architecture.",
            "response": "```mermaid\ngraph LR\nA[ava-agent] --> B[Redis]\n```",
            "intent": "architecture",
        }) == "ava-agent, PostgreSQL, Redis",
    )



    ava_self_models = ns["_answer_ava_self_query"](
        "What models are you running?",
        about={
            "containers": {},
            "models": {
                "llm": "qwen2.5:14b (Q4_K_M quantization)",
                "embedding": "nomic-embed-text",
                "vision": "llava:13b",
                "ollama_host": "http://host.docker.internal:11434",
            },
            "knowledge_base": {},
        },
    )
    check("ava self models are deterministic", "qwen2.5:14b" in ava_self_models and "nomic-embed-text" in ava_self_models and "llava:13b" in ava_self_models)
    ava_self_kb = ns["_answer_ava_self_query"](
        "What is your knowledge base size?",
        about={
            "containers": {},
            "models": {},
            "knowledge_base": {
                "devops_policies_v2": 3885,
                "devops_blogs_v1": 2513,
                "devops_fixes_v1": 20,
                "devops_patterns_v1": 64,
            },
        },
    )
    check("ava self kb total is computed", "6,482" in ava_self_kb and "devops_patterns_v1: 64" in ava_self_kb)
    check("oomkilled intent routes to troubleshooting", ns["detect_query_intent"]("What causes OOMKilled in Kubernetes?") == "troubleshooting")
    troubleshooting_resolved = ns["_resolve_troubleshooting_response"]("What causes OOMKilled in Kubernetes?")
    check("controlled troubleshooting response is deterministic", troubleshooting_resolved["response"].startswith("**Root Cause:**"))
    check("controlled troubleshooting strips blog noise", troubleshooting_resolved["sources_used"] == 2)
    architecture_resolved = ns["_resolve_architecture_response"]("Explain Netflix architecture with Zuul, Kafka, Cassandra, EVCache")
    check("controlled architecture response has sections", "**Components and Roles:**" in architecture_resolved["response"] and "**Request Flow:**" in architecture_resolved["response"])
    check("controlled architecture response strips noisy labels", "SUMMARY:" not in architecture_resolved["response"] and "REQUEST FLOW:" not in architecture_resolved["response"] and "kafka_consumer_lag" not in architecture_resolved["response"])
    architecture_diagram_resolved = ns["_resolve_architecture_response"]("Create a mermaid diagram of your Docker architecture")
    check("controlled architecture diagram is deterministic", architecture_diagram_resolved["response"].startswith("```mermaid"))
    check("controlled architecture diagram includes ava runtime", "ava-agent:5443" in architecture_diagram_resolved["response"])
    follow_up_resolved = ns["_resolve_follow_up_response"]("How is it different from the previous thing I asked?")
    check("controlled follow_up response is deterministic", "latest answer summary" in follow_up_resolved["response"].lower())
    comparison_resolved = ns["_resolve_comparison_response"]("What is the difference between readiness probe and liveness probe?")
    check("controlled comparison response includes both targets", "readiness probe" in comparison_resolved["response"].lower() and "liveness probe" in comparison_resolved["response"].lower())

    wrapped = '{"issue_type":"definition","command":"","risk_level":"","rollback":"","action_taken":"Readiness probe checks readiness. Liveness probe checks health."}'
    check("wrapper detected", ns["_looks_like_invalid_json_wrapper"](wrapped) is True)
    check(
        "wrapper repaired",
        ns["_repair_definition_wrapper"](wrapped) == "Readiness probe checks readiness. Liveness probe checks health.",
    )

    print("\nIntelligence regression tests passed.")


if __name__ == "__main__":
    main()
