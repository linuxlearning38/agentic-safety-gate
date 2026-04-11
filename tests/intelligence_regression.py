import ast
import json
import re
from datetime import datetime
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "web_agent_v2.1_guardrail.py"

FUNCTIONS = {
    "_normalize_text",
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
    "_build_follow_up_response",
    "_is_follow_up_query",
    "_is_healing_query",
    "_json_only_requested",
    "_predict_heal_action",
    "_format_playbook_template",
    "_build_healing_response",
    "_extract_query_entities",
    "_diagram_entities_from_text",
    "_hallucination_terms",
    "_repair_architecture_answer",
    "_build_grounded_architecture_answer",
    "_looks_generic_architecture_answer",
    "_extract_relevant_context_lines",
    "_is_noisy_architecture_line",
    "_filter_architecture_chunks",
    "_build_diagram_grounding_block",
    "_looks_like_mermaid_response",
    "_build_grounded_mermaid_diagram",
    "_repair_diagram_response",
    "_is_ava_self_architecture_query",
    "_diagram_needs_grounded_override",
    "_ava_runtime_diagram_entities",
    "_build_ava_runtime_diagram_response",
    "_looks_like_invalid_json_wrapper",
    "_repair_definition_wrapper",
    "_answer_known_incident_query",
    "_answer_ava_self_query",
    "detect_query_intent",
}

CONSTANTS = {
    "_MEMORY_FACT_KEY",
    "_ENTITY_STOP_WORDS",
    "_INFRA_COMPONENTS",
    "_GENERIC_HALLUCINATION_TERMS",
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
    def __init__(self, content):
        self.content = content


def load_helpers():
    src = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(SOURCE))
    namespace = {
        "json": json,
        "re": re,
        "datetime": datetime,
        "db": FakeDB(),
        "healer": FakeHealer(),
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

    fake_db.queries = [
        {"query": "Remember this exactly: cluster=prod-west-2", "response": "Okay", "intent": "memory"},
        {"query": "What is my cluster name?", "response": "Your cluster is prod-west-2.", "intent": "memory"},
        {"query": "What is the difference between readiness probe and liveness probe?", "response": "Readiness probe checks if a container is ready. Liveness probe checks if it is healthy enough to keep running.", "intent": "definition"},
    ]
    follow_up = ns["_build_follow_up_response"]("How is it different from the previous thing I asked?")
    check("follow up references latest topic", "readiness probe" in follow_up.lower())
    check("follow up references prior topic", "cluster name" in follow_up.lower())
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
    filtered_chunks = ns["_filter_architecture_chunks"](
        [
            FakeChunk("Zuul routes API requests to downstream services and Kafka carries events to Cassandra."),
            FakeChunk("terraform init"),
            FakeChunk("Kubernetes investigations and Kafka lag experiments"),
        ],
        ["Zuul", "Kafka", "Cassandra"],
    )
    check("architecture chunk filter keeps relationship-rich chunks", len(filtered_chunks) == 1 and "Zuul routes API requests" in filtered_chunks[0].content)
    relevant_lines = ns["_extract_relevant_context_lines"](
        ["Zuul routes API requests to Kafka.\nKafka writes events to Cassandra.\nKafka writes events to Cassandra."],
        ["Zuul", "Kafka", "Cassandra"],
    )
    check("relevant context lines return matches", len(relevant_lines) == 2)
    check("relevant context lines dedupe duplicates", relevant_lines[0] != "" and len(set(relevant_lines)) == len(relevant_lines))
    check("architecture noise line detected", ns["_is_noisy_architecture_line"]("# Queue depth: kafka_consumer_lag, redis_blocked_clients") is True)

    mermaid = ns["_build_grounded_mermaid_diagram"](
        ["Zuul handles API gateway traffic to Kafka", "Kafka carries events to Cassandra"],
        ["Zuul", "Kafka", "Cassandra"],
    )
    check("grounded mermaid generated", mermaid.startswith("```mermaid"))
    check("grounded mermaid keeps entities", "Zuul" in mermaid and "Kafka" in mermaid)
    ava_mermaid = ns["_build_grounded_mermaid_diagram"](
        ["ava-agent calls Ollama Host and uses Redis and PostgreSQL"],
        ["ava-agent", "Flask/Gunicorn", "PostgreSQL", "Redis", "Open Policy Agent", "HashiCorp Vault", "Ollama Host"],
    )
    check("ava mermaid uses preferred edges", "checks policy with" in ava_mermaid and "uses secrets from" in ava_mermaid)
    check(
        "ava self architecture query detected",
        ns["_is_ava_self_architecture_query"](
            "Create a mermaid diagram of your Docker architecture.",
            ["ava-agent", "Redis"],
        ) is True,
    )
    check(
        "generic docker diagram needs override",
        ns["_diagram_needs_grounded_override"](
            "```mermaid\ngraph LR\nA[Docker Host] --> B(Docker Daemon)\n```",
            ["ava-agent", "Redis", "PostgreSQL"],
        ) is True,
    )
    check(
        "ava runtime diagram entities are canonical",
        ns["_ava_runtime_diagram_entities"]() == [
            "ava-agent",
            "Flask/Gunicorn",
            "PostgreSQL",
            "Redis",
            "Open Policy Agent",
            "HashiCorp Vault",
            "Ollama Host",
        ],
    )
    ava_runtime_diagram = ns["_build_ava_runtime_diagram_response"]()
    check("ava runtime diagram is mermaid", ava_runtime_diagram.startswith("```mermaid"))
    check("ava runtime diagram includes policy edge", "checks policy with" in ava_runtime_diagram)
    check("ava runtime diagram includes ollama port", "11434" in ava_runtime_diagram)
    check(
        "topic from ava mermaid turn stays readable",
        ns["_topic_from_turn"]({
            "query": "Create a mermaid diagram of your Docker architecture.",
            "response": "```mermaid\ngraph LR\nA[ava-agent] --> B[Redis]\n```",
            "intent": "architecture",
        }) == "ava-agent, PostgreSQL, Redis",
    )

    repaired_diagram = ns["_repair_diagram_response"](
        "Zuul routes requests to Kafka.",
        ["Zuul routes requests to Kafka", "Kafka stores events in Cassandra"],
        ["Zuul", "Kafka", "Cassandra"],
    )
    check("diagram repair returns mermaid", ns["_looks_like_mermaid_response"](repaired_diagram) is True)

    hallucinations = ns["_hallucination_terms"](
        "This uses frontend, backend, and event sourcing heavily.",
        ["kafka", "zuul"],
    )
    check("hallucination terms detected", "frontend" in hallucinations and "backend" in hallucinations)

    repaired_arch = ns["_repair_architecture_answer"](
        "Microservices architecture\nFrontend handles users\nZuul is the gateway\nKafka handles events",
        ["Zuul", "Kafka"],
    )
    check("architecture repair keeps grounded entities", "Zuul" in repaired_arch and "Kafka" in repaired_arch)
    grounded_arch = ns["_build_grounded_architecture_answer"](
        [
            "Zuul routes API requests to backend services.",
            "# Queue depth: kafka_consumer_lag, redis_blocked_clients",
            "ARCHITECTURE REFERENCE: Kafka Design Overview",
            "Kafka carries playback events to Samza.",
            "Samza writes processed aggregates to Cassandra.",
            "EVCache caches hot reads for low latency.",
            "Mantis monitors streaming jobs and health.",
            "EXAMPLE: Netflix uses feature flags to enable new algorithms for 1% of users.",
            "Entities detected: cassandra, evcache, mantis, kafka, samza, zuul",
            "terraform init",
        ],
        ["Zuul", "Kafka", "Cassandra", "EVCache", "Samza", "Mantis"],
    )
    check("grounded architecture answer has request flow", "**Request Flow:**" in grounded_arch and "Zuul routes API requests" in grounded_arch)
    check("grounded architecture answer has data flow", "**Data Flow:**" in grounded_arch and "Kafka carries playback events" in grounded_arch)
    check("grounded architecture answer filters noise", "terraform init" not in grounded_arch and "Entities detected" not in grounded_arch and "kafka_consumer_lag" not in grounded_arch and "feature flags" not in grounded_arch and "Kafka Design Overview" not in grounded_arch)
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
    oom_answer = ns["_answer_known_incident_query"]("What causes OOMKilled in Kubernetes?")
    check("oomkilled answer is deterministic", oom_answer.startswith("**Root Cause:**") and "memory limit" in oom_answer)
    check(
        "generic architecture answer detected",
        ns["_looks_generic_architecture_answer"](
            "**Components:**\n- Kafka: Distributed Streaming Platform\n- EVCache: In-Memory Cache\n- Mantis: Monitoring and Alerting System"
        ) is True,
    )

    wrapped = '{"issue_type":"definition","command":"","risk_level":"","rollback":"","action_taken":"Readiness probe checks readiness. Liveness probe checks health."}'
    check("wrapper detected", ns["_looks_like_invalid_json_wrapper"](wrapped) is True)
    check(
        "wrapper repaired",
        ns["_repair_definition_wrapper"](wrapped) == "Readiness probe checks readiness. Liveness probe checks health.",
    )

    print("\nIntelligence regression tests passed.")


if __name__ == "__main__":
    main()
