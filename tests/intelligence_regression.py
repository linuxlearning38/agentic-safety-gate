import ast
import json
import re
from datetime import datetime
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "web_agent_v2.1_guardrail.py"

FUNCTIONS = {
    "_normalize_text",
    "_normalize_fact_key",
    "_fact_aliases",
    "_load_chat_facts",
    "_parse_memory_statement",
    "_extract_memory_request",
    "_extract_recall_label",
    "_recall_chat_fact",
    "_get_recent_distinct_turns",
    "_summarize_topic",
    "_build_follow_up_response",
    "_json_only_requested",
    "_predict_heal_action",
    "_format_playbook_template",
    "_build_healing_response",
    "_extract_query_entities",
    "_diagram_entities_from_text",
    "_hallucination_terms",
    "_repair_architecture_answer",
    "_extract_relevant_context_lines",
    "_build_diagram_grounding_block",
    "_looks_like_invalid_json_wrapper",
    "_repair_definition_wrapper",
}

CONSTANTS = {
    "_MEMORY_FACT_KEY",
    "_ENTITY_STOP_WORDS",
    "_INFRA_COMPONENTS",
    "_GENERIC_HALLUCINATION_TERMS",
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
        "memory recall alias",
        ns["_recall_chat_fact"]("cluster name")["value"] == "prod-west-2",
    )
    check(
        "recall label extract",
        ns["_extract_recall_label"]("What is my cluster name?") == "cluster name",
    )

    fake_db.queries = [
        {"query": "Remember this exactly: cluster=prod-west-2", "response": "Okay", "intent": "memory"},
        {"query": "What is my cluster name?", "response": "Your cluster is prod-west-2.", "intent": "memory"},
        {"query": "What is the difference between readiness probe and liveness probe?", "response": "Readiness probe checks if a container is ready. Liveness probe checks if it is healthy enough to keep running.", "intent": "definition"},
    ]
    follow_up = ns["_build_follow_up_response"]("How is it different from the previous thing I asked?")
    check("follow up references latest topic", "readiness probe" in follow_up.lower())
    check("follow up references prior topic", "cluster name" in follow_up.lower())

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

    wrapped = '{"issue_type":"definition","command":"","risk_level":"","rollback":"","action_taken":"Readiness probe checks readiness. Liveness probe checks health."}'
    check("wrapper detected", ns["_looks_like_invalid_json_wrapper"](wrapped) is True)
    check(
        "wrapper repaired",
        ns["_repair_definition_wrapper"](wrapped) == "Readiness probe checks readiness. Liveness probe checks health.",
    )

    print("\nIntelligence regression tests passed.")


if __name__ == "__main__":
    main()
