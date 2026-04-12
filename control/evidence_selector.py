from dataclasses import dataclass, field


@dataclass
class EvidencePacket:
    intent: str
    topic: str
    normalized_query: str
    facts: dict
    entities: list[str] = field(default_factory=list)
    evidence_blocks: list[str] = field(default_factory=list)


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
