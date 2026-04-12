from dataclasses import dataclass, field
from typing import Callable


AVA_SELF_TOPIC_PATTERNS = {
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

TROUBLESHOOTING_TOPIC_PATTERNS = {
    "oomkilled": ["oomkilled", "oom killed"],
    "crashloopbackoff": ["crashloopbackoff", "crashloop"],
    "imagepullbackoff": ["imagepullbackoff", "image pull backoff", "image pull error"],
    "pending": [" pending", "containercreating", "container creating"],
    "service_down": ["service is down", "service down", "unreachable", "can't connect", "cannot connect", "timeout"],
    "generic": ["error", "failed", "not working", "broken", "troubleshoot", "debug", "fix", "issue", "problem"],
}

EXPLICIT_EXECUTION_MARKERS = (
    "run kubectl", "execute", "apply the fix", "apply this", "run this",
    "run the", "kubectl apply", "kubectl exec", "diagnose now", "check now",
)


@dataclass
class IntentRoute:
    raw_query: str
    normalized_query: str
    intent: str
    confidence: str = "low"
    entities: list[str] = field(default_factory=list)
    topic: str | None = None
    memory_fact: dict | None = None
    memory_follow_up: str = ""
    recall_label: str | None = None
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
    if any(marker in q for marker in EXPLICIT_EXECUTION_MARKERS):
        return None
    for topic, patterns in TROUBLESHOOTING_TOPIC_PATTERNS.items():
        if any(pattern in q for pattern in patterns):
            return topic
    return None


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

    return IntentRoute(
        raw_query=raw_query,
        normalized_query=normalized_query,
        intent="unknown",
        confidence="low",
        entities=entities,
        reason="no controlled migration intent matched",
    )
