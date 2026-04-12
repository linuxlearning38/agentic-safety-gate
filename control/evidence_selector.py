from dataclasses import dataclass, field
import re


@dataclass
class EvidencePacket:
    intent: str
    topic: str
    normalized_query: str
    facts: dict
    entities: list[str] = field(default_factory=list)
    evidence_blocks: list[str] = field(default_factory=list)


ARCHITECTURE_RELATION_TERMS = {
    "route", "routes", "routing", "gateway", "front door", "edge",
    "request", "requests", "service", "services", "publish", "publishes",
    "consume", "consumes", "stream", "streams", "event", "events",
    "cache", "caches", "store", "stores", "read", "reads", "write", "writes",
    "process", "processes", "serve", "serves", "backend", "flow", "fan-out",
    "transport", "serving", "pipeline",
}

ARCHITECTURE_NOISE_MARKERS = (
    "queue depth", "consumer lag", "redis_blocked_clients", "kafka_consumer_lag",
    "slo:", "example:", "alert:", "checklist", "terraform init", "curl ",
    "kubectl ", "helm ", "latency:", "error rate", "30-day rolling",
)


def _strip_architecture_labels(text: str) -> str:
    return re.sub(
        r"(?im)^\s*(SUMMARY|REQUEST FLOW|DATA FLOW|ARCHITECTURE NOTES|ARCHITECTURE REFERENCE|TAGS):\s*",
        "",
        text or "",
    )


def _is_noisy_architecture_line(line: str) -> bool:
    lower = (line or "").strip().lower()
    if not lower:
        return True
    if lower.startswith("#"):
        return True
    if any(marker in lower for marker in ARCHITECTURE_NOISE_MARKERS):
        return True
    if re.search(r"\b[a-z]+_[a-z0-9_]+\b", lower) and not any(
        term in lower for term in ("gateway", "stream", "cache", "store", "event", "request", "route", "process")
    ):
        return True
    return False


def _extract_architecture_lines(text: str) -> list[str]:
    lines = []
    for raw_line in _strip_architecture_labels(text).splitlines():
        line = raw_line.strip(" -*\t")
        if _is_noisy_architecture_line(line):
            continue
        lines.append(line)
    return lines


def _architecture_line_score(line: str, entities: list[str], self_runtime: bool = False) -> int:
    lower = line.lower()
    entity_hits = sum(1 for entity in entities if entity.lower() in lower)
    relation_hits = sum(1 for term in ARCHITECTURE_RELATION_TERMS if term in lower)
    if self_runtime and "ava-agent" in lower:
        entity_hits += 1
    return entity_hits * 4 + relation_hits * 2 + min(len(line) // 80, 2)


def _runtime_architecture_blocks(about: dict) -> tuple[list[str], list[str]]:
    containers = about.get("containers", {})
    models = about.get("models", {})
    blocks = [
        "ava-agent serves requests through Flask/Gunicorn on port 5443.",
        "ava-agent reads and writes relational data in PostgreSQL on port 5432.",
        "ava-agent uses Redis on port 6379 for fast state and caching.",
        "ava-agent checks policy decisions with Open Policy Agent on port 8181.",
        "ava-agent reads secrets from HashiCorp Vault on port 8200.",
        f"ava-agent calls the Ollama host at {models.get('ollama_host', 'unknown')} for local model inference.",
    ]
    entities = list(containers.keys())
    if models.get("ollama_host"):
        entities.append("Ollama")
    return blocks, entities


def format_ava_self_facts_block(about: dict) -> str:
    containers = about.get("containers", {})
    models = about.get("models", {})
    kb = about.get("knowledge_base", {})
    lines = [
        "AVA System Facts:",
        f"version={about.get('version', 'unknown')} | phase={about.get('phase', 'unknown')} | built_by={about.get('built_by', 'unknown')} | runtime={about.get('runtime', 'unknown')}",
        "",
        "Containers and ports:",
    ]
    for name, info in containers.items():
        proto = f" ({info['proto']})" if info.get("proto") else ""
        lines.append(f"  {name}: port {info.get('port', 'unknown')}{proto} - {info.get('stack', 'unknown')}")
    lines.extend(
        [
            "",
            "Models:",
            f"  LLM: {models.get('llm', 'unknown')}",
            f"  Embedding: {models.get('embedding', 'unknown')}",
            f"  Vision: {models.get('vision', 'unknown')}",
            f"  Ollama host: {models.get('ollama_host', 'unknown')}",
            "",
            "Knowledge base chunks:",
        ]
    )
    for name, count in kb.items():
        lines.append(f"  {name}: {int(count or 0)}")
    return "\n".join(lines)


def select_ava_self_evidence(route, about: dict) -> EvidencePacket:
    return EvidencePacket(
        intent="ava_self",
        topic=route.topic or "runtime",
        normalized_query=route.normalized_query,
        facts=about,
        entities=list(route.entities or []),
        evidence_blocks=[format_ava_self_facts_block(about)],
    )


def select_memory_store_evidence(route) -> EvidencePacket:
    fact = route.memory_fact or {}
    facts = {
        "label": fact.get("label", ""),
        "value": fact.get("value", ""),
        "follow_up": route.memory_follow_up or "",
    }
    return EvidencePacket(
        intent="memory_store",
        topic="memory_store",
        normalized_query=route.normalized_query,
        facts=facts,
        entities=list(route.entities or []),
        evidence_blocks=[],
    )


def select_memory_recall_evidence(route, recalled_fact) -> EvidencePacket:
    facts = {
        "label": route.recall_label or "",
        "stored_fact": recalled_fact,
    }
    return EvidencePacket(
        intent="memory_recall",
        topic="memory_recall",
        normalized_query=route.normalized_query,
        facts=facts,
        entities=list(route.entities or []),
        evidence_blocks=[],
    )


def select_troubleshooting_evidence(route, retrieved_chunks: list) -> EvidencePacket:
    filtered_chunks = []
    evidence_blocks = []
    for chunk in retrieved_chunks or []:
        source_collection = getattr(chunk, "source_collection", "")
        if source_collection not in {"policies", "fixes"}:
            continue
        filtered_chunks.append(chunk)
        evidence_blocks.append(getattr(chunk, "content", ""))

    facts = {
        "topic": route.topic or "generic",
        "sources": [getattr(chunk, "source_collection", "") for chunk in filtered_chunks],
    }
    return EvidencePacket(
        intent="troubleshooting",
        topic=route.topic or "generic",
        normalized_query=route.normalized_query,
        facts=facts,
        entities=list(route.entities or []),
        evidence_blocks=evidence_blocks,
    )


def select_architecture_evidence(route, retrieved_chunks: list, about: dict | None = None) -> EvidencePacket:
    route_entities = list(route.entities or [])
    if route.topic == "self_runtime" and about:
        evidence_blocks, runtime_entities = _runtime_architecture_blocks(about)
        facts = {
            "topic": route.topic,
            "response_mode": route.response_mode,
            "sources": ["runtime_about"],
        }
        return EvidencePacket(
            intent="architecture",
            topic=route.topic or "external",
            normalized_query=route.normalized_query,
            facts=facts,
            entities=route_entities or runtime_entities,
            evidence_blocks=evidence_blocks,
        )

    scored_lines = []
    for chunk in retrieved_chunks or []:
        source_collection = getattr(chunk, "source_collection", "")
        if source_collection not in {"patterns", "blogs", "policies", "fixes"}:
            continue
        text = getattr(chunk, "content", "") or ""
        lines = _extract_architecture_lines(text)
        for line in lines:
            score = _architecture_line_score(line, route_entities)
            lower = line.lower()
            entity_hits = sum(1 for entity in route_entities if entity.lower() in lower)
            relation_hits = sum(1 for term in ARCHITECTURE_RELATION_TERMS if term in lower)
            if entity_hits >= 2 or (entity_hits >= 1 and relation_hits >= 1) or (not route_entities and relation_hits >= 2) or (len(route_entities) <= 1 and relation_hits >= 3):
                scored_lines.append((score, line, source_collection))

    scored_lines.sort(key=lambda item: (-item[0], len(item[1])))
    evidence_blocks = []
    evidence_sources = []
    seen = set()
    for _, line, source_collection in scored_lines:
        lowered = line.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        evidence_blocks.append(line)
        evidence_sources.append(source_collection)
        if len(evidence_blocks) >= 8:
            break

    facts = {
        "topic": route.topic or "external",
        "response_mode": route.response_mode,
        "sources": evidence_sources,
    }
    return EvidencePacket(
        intent="architecture",
        topic=route.topic or "external",
        normalized_query=route.normalized_query,
        facts=facts,
        entities=route_entities,
        evidence_blocks=evidence_blocks,
    )


def select_follow_up_evidence(route, recent_turns: list, topic_fn, summary_fn) -> EvidencePacket:
    recent_turns = list(recent_turns or [])
    last_turn = recent_turns[-1] if recent_turns else None
    previous_turn = recent_turns[-2] if len(recent_turns) >= 2 else None
    facts = {
        "last_turn": last_turn,
        "previous_turn": previous_turn,
        "last_topic": topic_fn(last_turn) if last_turn else "",
        "previous_topic": topic_fn(previous_turn) if previous_turn else "",
        "last_summary": summary_fn((last_turn or {}).get("response", "")) if last_turn else "",
    }
    evidence_blocks = []
    if last_turn:
        evidence_blocks.append((last_turn.get("response") or "").strip())
    if previous_turn:
        evidence_blocks.append((previous_turn.get("response") or "").strip())
    return EvidencePacket(
        intent="follow_up",
        topic="follow_up",
        normalized_query=route.normalized_query,
        facts=facts,
        entities=list(route.entities or []),
        evidence_blocks=evidence_blocks,
    )


def select_comparison_evidence(route, retrieved_chunks: list) -> EvidencePacket:
    targets = list(route.comparison_targets or [])
    target_lowers = [target.lower() for target in targets]
    collected = {target.lower(): [] for target in targets}
    evidence_blocks = []
    seen = set()

    for chunk in retrieved_chunks or []:
        text = _strip_architecture_labels(getattr(chunk, "content", "") or "")
        for raw_line in text.splitlines():
            line = raw_line.strip(" -*\t")
            if not line:
                continue
            lowered = line.lower()
            if lowered in seen:
                continue
            if any(marker in lowered for marker in ARCHITECTURE_NOISE_MARKERS):
                continue
            for target in target_lowers:
                if target in lowered and len(collected[target]) < 3:
                    collected[target].append(line)
                    seen.add(lowered)
                    evidence_blocks.append(line)
                    break

    facts = {
        "targets": targets,
        "collected": collected,
        "sources": [getattr(chunk, "source_collection", "") for chunk in (retrieved_chunks or [])],
    }
    return EvidencePacket(
        intent="comparison",
        topic="comparison",
        normalized_query=route.normalized_query,
        facts=facts,
        entities=list(route.entities or []),
        evidence_blocks=evidence_blocks,
    )


def select_definition_evidence(route, retrieved_chunks: list) -> EvidencePacket:
    target_entities = [entity for entity in (route.entities or []) if entity]
    evidence_blocks = []
    seen = set()

    for chunk in retrieved_chunks or []:
        text = _strip_architecture_labels(getattr(chunk, "content", "") or "")
        for raw_line in text.splitlines():
            line = raw_line.strip(" -*\t")
            if not line:
                continue
            lowered = line.lower()
            if lowered in seen:
                continue
            if any(marker in lowered for marker in ARCHITECTURE_NOISE_MARKERS):
                continue
            if target_entities and not any(entity.lower() in lowered for entity in target_entities):
                continue
            seen.add(lowered)
            evidence_blocks.append(line)
            if len(evidence_blocks) >= 4:
                break
        if len(evidence_blocks) >= 4:
            break

    facts = {
        "query": route.normalized_query,
        "sources": [getattr(chunk, "source_collection", "") for chunk in (retrieved_chunks or [])],
    }
    return EvidencePacket(
        intent="definition",
        topic="definition",
        normalized_query=route.normalized_query,
        facts=facts,
        entities=target_entities,
        evidence_blocks=evidence_blocks,
    )
