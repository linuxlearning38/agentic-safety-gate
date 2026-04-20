from dataclasses import dataclass, field
import re
from typing import Callable


AVA_SELF_TOPIC_PATTERNS = {
    "name": [
        "what is your name", "what's your name", "who are you",
        "your name", "are you ava", "what are you called",
    ],
    "authorship": [
        "who built you", "who made you", "who created you", "who developed you",
        "what are you made of",
    ],
    "safety": [
        "are you safe to use", "are you safe", "is ava safe",
    ],
    "containers": [
        "what containers", "your containers", "your docker", "your ports",
        "what ports", "services and ports", "what services", "your services",
        "docker containers", "containers and ports",
    ],
    "models": [
        "what models", "which model", "your model", "your models",
        "are you running", "do you run", "what are you running",
    ],
    "knowledge_base": [
        "knowledge base size", "how many chunks", "your knowledge",
        "knowledge base", "kb size",
    ],
    "runtime": [
        "runtime", "your stack", "overview", "how do you work",
        "your pipeline", "your files", "your database",
    ],
}

ARCHITECTURE_TOPIC_PATTERNS = {
    "self_runtime": [
        "your docker architecture", "your architecture", "ava architecture",
        "docker architecture", "your runtime architecture", "your container architecture",
    ],
    "external": [
        "architecture", "request flow", "data flow", "system design",
        "topology", "deployment flow", "sequence flow", "component flow",
    ],
}

DIAGRAM_MARKERS = (
    "diagram", "mermaid", "draw", "visualize", "topology", "flowchart",
    "flow chart", "sequence", "graph ",
)

TROUBLESHOOTING_TOPIC_PATTERNS = {
    "oomkilled": ["oomkilled", "oom killed"],
    "crashloopbackoff": ["crashloopbackoff", "crashloop"],
    "imagepullbackoff": ["imagepullbackoff", "image pull backoff", "image pull error"],
    "pending": [" pending", "containercreating", "container creating"],
    "pod_network": ["pod network", "pod networking", "network policy", "cni"],
    "dns_failure": ["dns failure", "dns issue", "dns problem", "dns resolution", "coredns", "service discovery"],
    "tls_certificate": ["tls certificate", "tls cert", "certificate issue", "certificate error", "ssl certificate", "cert expiry", "certificate expiry"],
    "service_down": ["service is down", "service down", "unreachable", "can't connect", "cannot connect", "timeout"],
    "generic": ["error", "failed", "failing", "not working", "broken", "troubleshoot", "debug", "fix", "issue", "problem"],
}

EXPLICIT_EXECUTION_MARKERS = (
    "run kubectl", "execute", "apply the fix", "apply this", "run this",
    "run the", "kubectl apply", "kubectl exec", "diagnose now", "check now",
)

FOLLOW_UP_MARKERS = (
    "previous thing", "previous question", "previous thing i asked",
    "previous thing i said", "what did i just say", "what did i just ask",
    "different from the previous", "different from what i asked",
    "what we discussed", "compare that with",
    "do that", "do it", "run that", "run it", "apply that", "apply it",
    "fix it", "what should i do next", "what is the next step",
    "run the next step", "continue with that",
)

COMPARISON_MARKERS = (
    "difference between", "compare", " versus ", " vs ", "compare ",
)

DEFINITION_MARKERS = (
    "what is ", "what are ", "define ", "explain ",
)

STRONG_DEVOPS_MARKERS = (
    "kubernetes", "k8s", "docker", "helm", "terraform", "configmap",
    "oomkilled", "crashloopbackoff", "crashloop", "imagepullbackoff", "imagepull",
    "ingress", "pvc", "istio", "grafana", "prometheus", "vault", "opa", "kubectl",
    "dockerfile", "kubeconfig", "pod", "namespace", "deployment", "daemonset",
    "statefulset", "replicaset", "cronjob", "readiness", "liveness", "probe", "linux",
)

WEAK_DEVOPS_MARKERS = (
    "network", "security", "memory", "cpu", "disk", "server", "service",
    "git", "container", "cluster", "node", "pipeline", "ci", "cd",
    "monitoring", "logging", "ssl", "tls", "jwt", "redis", "postgres",
    "kafka", "cassandra", "nginx", "linux", "aws", "azure", "gcp",
)

ACTION_STYLE_MARKERS = (
    "delete", "remove", "destroy", "drop", "truncate", "wipe", "shutdown",
    "restart", " stop ", "terminate", " kill ", "run ", "execute", "apply ",
    "sudo ", "chmod ", "chown ", "rm ",
    "del ", "format ", "curl ", "wget ", "ssh ", "scp ", "powershell ", "cmd /c",
    "bash ", "write a command", "give me a command", "rollout", "scale ",
)

OPERATIONAL_CONTEXT_MARKERS = (
    "my ", "our ", "in kubernetes", "in docker", "in production", "failing",
    "failed", "not working", "broken", "issue", "problem", "debug", "fix",
    "deploy", "deployment", "pod", "container", "cluster", "namespace",
    "service", "node", "pipeline", "image", "registry", "volume", "pvc",
)


@dataclass
class IntentRoute:
    raw_query: str
    normalized_query: str
    intent: str | None
    confidence: str = "low"
    entities: list[str] = field(default_factory=list)
    topic: str | None = None
    memory_fact: dict | None = None
    memory_follow_up: str = ""
    recall_label: str | None = None
    comparison_targets: list[str] = field(default_factory=list)
    response_mode: str = "text"
    reason: str = ""


def classify_ava_self_topic(normalized_query: str) -> str | None:
    q = (normalized_query or "").lower().strip()
    for topic, patterns in AVA_SELF_TOPIC_PATTERNS.items():
        if any(pattern in q for pattern in patterns):
            return topic
    if "ava" in q:
        return "runtime"
    return None


def classify_troubleshooting_topic(normalized_query: str) -> str | None:
    q = f" {(normalized_query or '').lower().strip()} "
    if any(q.strip().startswith(marker.strip()) for marker in DEFINITION_MARKERS):
        return None
    if any(marker in q for marker in EXPLICIT_EXECUTION_MARKERS):
        return None
    for topic, patterns in TROUBLESHOOTING_TOPIC_PATTERNS.items():
        if any(pattern in q for pattern in patterns):
            return topic
    return None


def classify_architecture_topic(normalized_query: str, entities: list[str] | None = None) -> tuple[str, str] | None:
    q = f" {(normalized_query or '').lower().strip()} "
    entities = [entity for entity in (entities or []) if entity]
    wants_diagram = any(marker in q for marker in DIAGRAM_MARKERS)
    has_architecture_language = any(
        pattern in q
        for patterns in ARCHITECTURE_TOPIC_PATTERNS.values()
        for pattern in patterns
    )
    if not wants_diagram and not has_architecture_language:
        return None

    self_runtime_markers = ARCHITECTURE_TOPIC_PATTERNS["self_runtime"]
    topic = "self_runtime" if any(marker in q for marker in self_runtime_markers) else "external"
    if topic != "self_runtime" and " ava " in q and any(word in q for word in ("your", "docker", "runtime", "container")):
        topic = "self_runtime"
    if topic != "self_runtime" and not wants_diagram and len(entities) < 2 and "architecture" not in q and "flow" not in q:
        return None
    response_mode = "diagram" if wants_diagram else "text"
    return topic, response_mode


def is_follow_up_query(normalized_query: str) -> bool:
    q = (normalized_query or "").lower().strip()
    return any(marker in q for marker in FOLLOW_UP_MARKERS)


def _clean_comparison_target(text: str) -> str:
    cleaned = (text or "").strip().strip("?.!, ")
    cleaned = re.sub(
        r"^(what is the difference between|difference between|compare|explain|describe|tell me about)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def extract_comparison_targets(normalized_query: str) -> list[str]:
    q = (normalized_query or "").strip().rstrip("?.!")
    q_lower = q.lower()
    match = re.search(r"difference between\s+(.+?)\s+and\s+(.+)$", q_lower, re.IGNORECASE)
    if match:
        left = match.group(1).strip()
        right = match.group(2).strip()
        return [_clean_comparison_target(left), _clean_comparison_target(right)]
    if " vs " in q_lower:
        parts = re.split(r"\s+vs\s+", q, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            return [_clean_comparison_target(parts[0]), _clean_comparison_target(parts[1])]
    if " versus " in q_lower:
        parts = re.split(r"\s+versus\s+", q, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            return [_clean_comparison_target(parts[0]), _clean_comparison_target(parts[1])]
    match = re.search(r"compare\s+(.+?)\s+and\s+(.+)$", q, re.IGNORECASE)
    if match:
        return [_clean_comparison_target(match.group(1)), _clean_comparison_target(match.group(2))]
    return []


def classify_definition_topic(normalized_query: str) -> str | None:
    q = (normalized_query or "").strip().lower()
    if any(marker in q for marker in COMPARISON_MARKERS):
        return None
    if any(q.startswith(marker) for marker in DEFINITION_MARKERS):
        return "definition"
    return None


def is_action_style_query(normalized_query: str) -> bool:
    q = f" {(normalized_query or '').lower().strip()} "
    return any(marker in q for marker in ACTION_STYLE_MARKERS)


def devops_domain_score(normalized_query: str) -> tuple[int, int, int]:
    q = f" {(normalized_query or '').lower().strip()} "
    strong_hits = sum(1 for marker in STRONG_DEVOPS_MARKERS if marker in q)
    weak_hits = sum(1 for marker in WEAK_DEVOPS_MARKERS if marker in q)
    return strong_hits * 2 + weak_hits, strong_hits, weak_hits


def is_grounded_devops_query(normalized_query: str) -> bool:
    q = (normalized_query or "").lower().strip()
    score, strong_hits, weak_hits = devops_domain_score(q)
    if strong_hits >= 1:
        return True
    if score < 2:
        return False
    return any(marker in q for marker in OPERATIONAL_CONTEXT_MARKERS) or weak_hits >= 3


def route_query(
    query: str,
    *,
    normalizer: Callable[[str], str] | None = None,
    entity_extractor: Callable[[str], list[str]] | None = None,
    memory_request_extractor: Callable[[str], dict | None] | None = None,
    recall_label_extractor: Callable[[str], str | None] | None = None,
) -> IntentRoute:
    raw_query = query or ""
    normalized_query = normalizer(raw_query) if normalizer else raw_query.strip()
    normalized_query = (normalized_query or "").strip()
    entities = entity_extractor(normalized_query) if entity_extractor else []

    if memory_request_extractor:
        memory_request = memory_request_extractor(normalized_query)
        if memory_request:
            fact = memory_request.get("fact") or {}
            return IntentRoute(
                raw_query=raw_query,
                normalized_query=normalized_query,
                intent="memory_store",
                confidence="high",
                entities=entities,
                topic="memory_store",
                memory_fact=fact,
                memory_follow_up=(memory_request.get("follow_up") or "").strip(),
                reason="matched deterministic memory store request",
            )

    if recall_label_extractor:
        recall_label = recall_label_extractor(normalized_query)
        if recall_label:
            return IntentRoute(
                raw_query=raw_query,
                normalized_query=normalized_query,
                intent="memory_recall",
                confidence="high",
                entities=entities,
                topic="memory_recall",
                recall_label=recall_label,
                reason="matched deterministic memory recall request",
            )

    architecture_match = classify_architecture_topic(normalized_query, entities)
    if architecture_match:
        architecture_topic, response_mode = architecture_match
        return IntentRoute(
            raw_query=raw_query,
            normalized_query=normalized_query,
            intent="architecture",
            confidence="high" if architecture_topic == "self_runtime" else "medium",
            entities=entities,
            topic=architecture_topic,
            response_mode=response_mode,
            reason=f"matched architecture topic '{architecture_topic}' in {response_mode} mode",
        )

    ava_self_topic = classify_ava_self_topic(normalized_query)
    if ava_self_topic:
        return IntentRoute(
            raw_query=raw_query,
            normalized_query=normalized_query,
            intent="ava_self",
            confidence="high",
            entities=entities,
            topic=ava_self_topic,
            reason=f"matched ava_self topic '{ava_self_topic}'",
        )

    if is_follow_up_query(normalized_query):
        return IntentRoute(
            raw_query=raw_query,
            normalized_query=normalized_query,
            intent="follow_up",
            confidence="high",
            entities=entities,
            topic="follow_up",
            reason="matched controlled follow-up request",
        )

    if is_action_style_query(normalized_query):
        return IntentRoute(
            raw_query=raw_query,
            normalized_query=normalized_query,
            intent=None,
            confidence="medium",
            entities=entities,
            reason="action-style request bypasses general_qwen routing",
        )

    troubleshooting_topic = classify_troubleshooting_topic(normalized_query)
    if troubleshooting_topic:
        return IntentRoute(
            raw_query=raw_query,
            normalized_query=normalized_query,
            intent="troubleshooting",
            confidence="high" if troubleshooting_topic != "generic" else "medium",
            entities=entities,
            topic=troubleshooting_topic,
            reason=f"matched troubleshooting topic '{troubleshooting_topic}'",
        )

    comparison_targets = extract_comparison_targets(normalized_query)
    if comparison_targets:
        if not is_grounded_devops_query(normalized_query):
            return IntentRoute(
                raw_query=raw_query,
                normalized_query=normalized_query,
                intent="general_qwen",
                confidence="medium",
                entities=entities,
                topic="general_qwen",
                comparison_targets=comparison_targets,
                reason="comparison request classified outside grounded DevOps domain",
            )
        return IntentRoute(
            raw_query=raw_query,
            normalized_query=normalized_query,
            intent="comparison",
            confidence="medium",
            entities=entities,
            topic="comparison",
            comparison_targets=comparison_targets,
            reason="matched controlled comparison request",
        )

    definition_topic = classify_definition_topic(normalized_query)
    if definition_topic:
        if not is_grounded_devops_query(normalized_query):
            return IntentRoute(
                raw_query=raw_query,
                normalized_query=normalized_query,
                intent="general_qwen",
                confidence="medium",
                entities=entities,
                topic="general_qwen",
                reason="definition request classified outside grounded DevOps domain",
            )
        return IntentRoute(
            raw_query=raw_query,
            normalized_query=normalized_query,
            intent="definition",
            confidence="medium",
            entities=entities,
            topic=definition_topic,
            reason="matched controlled definition request",
        )

    if not is_grounded_devops_query(normalized_query):
        return IntentRoute(
            raw_query=raw_query,
            normalized_query=normalized_query,
            intent="general_qwen",
            confidence="medium",
            entities=entities,
            topic="general_qwen",
            reason="query classified outside grounded DevOps domain",
        )

    return IntentRoute(
        raw_query=raw_query,
        normalized_query=normalized_query,
        intent="unknown",
        confidence="low",
        entities=entities,
        reason="no controlled migration intent matched",
    )
