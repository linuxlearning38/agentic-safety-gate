from dataclasses import dataclass, field


@dataclass
class AnswerPlan:
    intent: str
    mode: str
    topic: str
    answer: str
    evidence: dict = field(default_factory=dict)
    entities: list[str] = field(default_factory=list)
    confidence: str = "high"


def build_ava_self_plan(route, evidence) -> AnswerPlan:
    about = evidence.facts
    containers = about.get("containers", {})
    models = about.get("models", {})
    kb = about.get("knowledge_base", {})
    total_chunks = sum(int(count or 0) for count in kb.values())

    if evidence.topic == "containers":
        lines = ["I run these Docker containers and ports:"]
        for name, data in containers.items():
            proto = f" ({data['proto']})" if data.get("proto") else ""
            lines.append(f"- {name}: port {data.get('port', 'unknown')}{proto} - {data.get('stack', 'unknown')}")
        answer = "\n".join(lines)
    elif evidence.topic == "models":
        answer = (
            "I run these local models via Ollama:\n"
            f"- LLM: {models.get('llm', 'unknown')}\n"
            f"- Embedding: {models.get('embedding', 'unknown')}\n"
            f"- Vision: {models.get('vision', 'unknown')}\n"
            f"- Ollama host: {models.get('ollama_host', 'unknown')}"
        )
    elif evidence.topic == "knowledge_base":
        lines = [f"My knowledge base currently has {total_chunks:,} chunks across {len(kb)} collections:"]
        for name, count in kb.items():
            lines.append(f"- {name}: {int(count or 0):,} chunks")
        answer = "\n".join(lines)
    else:
        answer = (
            f"I am AVA {about.get('version', '')}, built by {about.get('built_by', 'unknown')}.\n"
            f"My runtime is {about.get('runtime', 'unknown')}.\n"
            f"My main app container is ava-agent on port 5443, and I use {models.get('llm', 'unknown')} as my main LLM."
        )

    return AnswerPlan(
        intent="ava_self",
        mode="deterministic",
        topic=evidence.topic,
        answer=answer,
        evidence=about,
        entities=list(evidence.entities or []),
        confidence="high",
    )


def build_memory_store_plan(route, evidence, saved_fact: dict) -> AnswerPlan:
    follow_up = evidence.facts.get("follow_up", "")
    if follow_up:
        answer = f"Your {saved_fact['label']} is {saved_fact['value']}."
    else:
        answer = f"Okay — I'll remember that your {saved_fact['label']} is {saved_fact['value']}."
    return AnswerPlan(
        intent="memory_store",
        mode="deterministic",
        topic="memory_store",
        answer=answer,
        evidence={"saved_fact": saved_fact},
        confidence="high",
    )


def build_memory_recall_plan(route, evidence) -> AnswerPlan:
    fact = evidence.facts.get("stored_fact")
    if fact:
        answer = f"Your {fact['label']} is {fact['value']}."
        confidence = "high"
    else:
        answer = f"I don't have a stored value for your {evidence.facts.get('label', route.recall_label or 'memory')} yet."
        confidence = "low"
    return AnswerPlan(
        intent="memory_recall",
        mode="deterministic",
        topic="memory_recall",
        answer=answer,
        evidence={"stored_fact": fact},
        confidence=confidence,
    )
