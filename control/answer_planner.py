from dataclasses import dataclass, field
import re


@dataclass
class AnswerPlan:
    intent: str
    mode: str
    topic: str
    answer: str
    evidence: dict = field(default_factory=dict)
    entities: list[str] = field(default_factory=list)
    confidence: str = "high"


ARCHITECTURE_ROLE_HINTS = {
    "zuul": "edge gateway that receives client traffic and routes requests to backend services",
    "kafka": "event backbone that carries durable streams between producers and consumers",
    "cassandra": "durable serving store for large-scale operational or derived data",
    "evcache": "hot-read cache that reduces latency in front of durable stores",
    "samza": "stream processor that consumes Kafka topics and computes derived views",
    "mantis": "stream processing and observability system for real-time analytics",
    "ava-agent": "main AVA application container that serves API requests",
    "agent_postgres": "relational database for durable AVA state",
    "agent_redis": "cache and fast state store",
    "agent_opa": "policy engine for authorization decisions",
    "agent_vault": "secret manager for sensitive runtime configuration",
    "ollama": "local model host used for inference",
}

PLAIN_COMPANY_NAMES = {"netflix", "uber", "twitter", "x", "meta", "amazon", "google"}
ROLE_HINT_TERMS = {"gateway", "edge", "cache", "store", "stream", "processor", "service", "database", "front door", "transport"}


def _first_sentence(text: str) -> str:
    match = re.split(r"(?<=[.!?])\s+", (text or "").strip(), maxsplit=1)
    return match[0].strip()


def _clean_explanation_line(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^(TRADEOFFS|BENEFITS|DESCRIPTION|EXAMPLE|IMPLEMENTATION):\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(copy \| explain|read more|certified kubernetes administrator course.*?)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")
    return cleaned


def _looks_like_noise(text: str) -> bool:
    lower = (text or "").lower()
    if not lower:
        return True
    noise_markers = (
        "copy | explain",
        "read more",
        "certified kubernetes administrator course",
        "a comprehensive guide introduction to kubernetes",
        "trains are created",
        "kill the main processes",
        "used to kill the processes forcefully",
    )
    return any(marker in lower for marker in noise_markers)


def _pick_architecture_lines(evidence_blocks: list[str], entities: list[str], *terms: str) -> list[str]:
    picked = []
    seen = set()
    for line in evidence_blocks or []:
        lower = line.lower()
        if terms and not any(term in lower for term in terms):
            continue
        if entities and not any(entity.lower() in lower for entity in entities):
            continue
        if lower in seen:
            continue
        seen.add(lower)
        picked.append(_first_sentence(line))
        if len(picked) >= 4:
            break
    return picked


def _component_role(entity: str, evidence_blocks: list[str]) -> str:
    entity_lower = entity.lower()
    for line in evidence_blocks or []:
        if entity_lower in line.lower():
            sentence = _first_sentence(line)
            if len(sentence) >= 20:
                return sentence
    return ARCHITECTURE_ROLE_HINTS.get(entity_lower, f"{entity} participates in the architecture flow shown by the retrieved evidence.")


def _should_keep_architecture_entity(entity: str, evidence_blocks: list[str]) -> bool:
    entity_lower = entity.lower()
    if entity_lower not in PLAIN_COMPANY_NAMES:
        return True
    for line in evidence_blocks or []:
        lower = line.lower()
        if entity_lower in lower and any(term in lower for term in ROLE_HINT_TERMS):
            return True
    return False


def _build_architecture_mermaid(entities: list[str], evidence_blocks: list[str], topic: str) -> str:
    lower_entities = {entity.lower(): entity for entity in entities}
    if topic == "self_runtime":
        return (
            "```mermaid\n"
            "graph LR\n"
            "    Client[\"User Request\"] --> Ava[\"ava-agent:5443\\nFlask/Gunicorn\"]\n"
            "    Ava --> Postgres[\"PostgreSQL:5432\"]\n"
            "    Ava --> Redis[\"Redis:6379\"]\n"
            "    Ava --> OPA[\"Open Policy Agent:8181\"]\n"
            "    Ava --> Vault[\"HashiCorp Vault:8200\"]\n"
            "    Ava --> Ollama[\"Ollama Host\"]\n"
            "```"
        )

    edges = []
    if "zuul" in lower_entities:
        edges.append(("Client", lower_entities["zuul"]))
        edges.append((lower_entities["zuul"], "Backend Services"))
    if "kubernetes" in lower_entities:
        edges.extend([
            ("Client", "Ingress / Gateway"),
            ("Ingress / Gateway", "Kubernetes Service"),
            ("Kubernetes Service", "Pods"),
            ("Deployment", "ReplicaSet"),
            ("ReplicaSet", "Pods"),
            ("Pods", "Container"),
        ])
    if "kafka" in lower_entities:
        edges.append(("Backend Services", lower_entities["kafka"]))
    if "samza" in lower_entities and "kafka" in lower_entities:
        edges.append((lower_entities["kafka"], lower_entities["samza"]))
    if "cassandra" in lower_entities and "kafka" in lower_entities:
        source = lower_entities.get("samza", lower_entities["kafka"])
        edges.append((source, lower_entities["cassandra"]))
    if "evcache" in lower_entities:
        cache_source = lower_entities.get("cassandra", "Backend Services")
        edges.append((cache_source, lower_entities["evcache"]))
        edges.append((lower_entities["evcache"], "Clients"))

    if not edges and len(entities) >= 2:
        ordered = entities[:]
        edges = list(zip(ordered, ordered[1:]))
    if not edges:
        edges = [
            ("Clients", "Ingress / Gateway"),
            ("Ingress / Gateway", "Application Service"),
            ("Application Service", "Data Store"),
        ]

    lines = ["```mermaid", "graph LR"]
    for left, right in edges[:8]:
        lines.append(f"    {re.sub(r'[^A-Za-z0-9]+', '', left) or 'NodeA'}[\"{left}\"] --> {re.sub(r'[^A-Za-z0-9]+', '', right) or 'NodeB'}[\"{right}\"]")
    lines.append("```")
    return "\n".join(lines)


def _build_lifecycle_mermaid(normalized_query: str, entities: list[str]) -> str:
    q = (normalized_query or "").lower()
    if "kubernetes" in q or "docker" in q or "devops" in q or "lifecycle" in q:
        return (
            "```mermaid\n"
            "graph LR\n"
            "    Plan[\"Plan\"] --> Code[\"Code\"]\n"
            "    Code --> Build[\"Build\"]\n"
            "    Build --> Docker[\"Docker Image\"]\n"
            "    Docker --> Test[\"Test / Scan\"]\n"
            "    Test --> Registry[\"Image Registry\"]\n"
            "    Registry --> Deploy[\"Deploy\"]\n"
            "    Deploy --> Kubernetes[\"Kubernetes Cluster\"]\n"
            "    Kubernetes --> Observe[\"Observe / Monitor\"]\n"
            "    Observe --> Plan\n"
            "```"
        )
    return _build_architecture_mermaid(entities, [], "external")


def build_ava_self_plan(route, evidence) -> AnswerPlan:
    about = evidence.facts
    containers = about.get("containers", {})
    models = about.get("models", {})
    kb = about.get("knowledge_base", {})
    total_chunks = sum(int(count or 0) for count in kb.values())

    if evidence.topic == "name":
        answer = (
            f"My name is AVA.\n"
            f"I am the secured DevOps assistant running as AVA {about.get('version', '')}."
        )
    elif evidence.topic == "authorship":
        answer = (
            "AVA was built by Manoj (DevOps engineer, Delhi) as a secured local DevOps assistant.\n"
            "The reasoning engine is Qwen 2.5 14B via Ollama, but AVA's routing, approval logic, "
            "and security policies are all original."
        )
    elif evidence.topic == "safety":
        answer = (
            "Yes. AVA uses approval gating for medium-risk actions, blocks critical destructive commands "
            "(rm -rf /, drop tables, format /dev/), uses a unified execution authority, and has an audit trail.\n"
            "AVA's safety cage is what makes it different from giving raw shell access to an LLM."
        )
    elif evidence.topic == "containers":
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


def build_troubleshooting_plan(route, evidence) -> AnswerPlan:
    topic = evidence.topic or route.topic or "generic"
    canned_answers = {
        "oomkilled": (
            "**Root Cause:** Kubernetes marks a container as `OOMKilled` when it exceeds its memory limit and the kernel terminates it to protect node stability.\n"
            "**Fix:** Raise the pod memory limit if it is too low, reduce the application's memory use, and compare actual peak usage against the current request and limit.\n"
            "**Why this works:** `OOMKilled` is a memory-pressure termination, so the durable fix is aligning the workload's memory behavior with Kubernetes limits.\n"
            "**Watch out for:** Restarts can hide the symptom temporarily. Check for memory leaks, bursty traffic, large in-memory caches, or JVM heap settings before only increasing limits."
        ),
        "crashloopbackoff": (
            "**Root Cause:** `CrashLoopBackOff` means the container keeps starting, failing, and being restarted, so Kubernetes backs off between restart attempts.\n"
            "**Fix:** Check the container logs, last termination reason, image entrypoint, env vars, config mounts, and readiness or liveness probe settings. Common causes are bad startup commands, missing config, and application crashes.\n"
            "**Why this works:** `CrashLoopBackOff` is a restart pattern, not the root problem itself. The real fix comes from the failing process or probe.\n"
            "**Watch out for:** If probes are too aggressive, Kubernetes can restart an otherwise healthy app before it finishes booting."
        ),
        "imagepullbackoff": (
            "**Root Cause:** `ImagePullBackOff` means Kubernetes could not pull the container image and is backing off before retrying.\n"
            "**Fix:** Verify the image name and tag, confirm the registry is reachable, and ensure the pod has valid image pull credentials if the registry is private.\n"
            "**Why this works:** The pod cannot start until the image is fetched successfully, so fixing the image reference or authentication removes the startup blocker.\n"
            "**Watch out for:** A mutable tag can hide the real issue. Confirm the exact image digest or expected tag during rollback and redeploy."
        ),
        "pending": (
            "**Root Cause:** A pod in `Pending` has not been scheduled or cannot start because the cluster is still missing resources, volumes, or other required dependencies.\n"
            "**Fix:** Check scheduler events, resource requests, node availability, PVC binding, taints, tolerations, and admission policy failures.\n"
            "**Why this works:** `Pending` is usually a placement or dependency problem, so the durable fix comes from the scheduler events and missing prerequisite.\n"
            "**Watch out for:** Low cluster capacity can make `Pending` look intermittent. Check quotas and autoscaler behavior before changing only the pod spec."
        ),
        "pod_network": (
            "**Root Cause:** Pod network failures usually come from CNI health, NetworkPolicy rules, DNS, Service selectors/endpoints, node routing, or an application binding to the wrong interface.\n"
            "**Fix:** Check the Pod IP, Service endpoints, DNS lookup from another Pod, NetworkPolicy denies, CNI plugin health, node routes, and recent cluster networking changes.\n"
            "**Why this works:** Pod-to-Pod and Pod-to-Service traffic crosses several layers, so checking each hop isolates whether the failure is policy, DNS, routing, or workload readiness.\n"
            "**Watch out for:** Restarting the Pod can hide a network-policy or CNI problem. Confirm the traffic path before changing the workload."
        ),
        "service_down": (
            "**Root Cause:** A service being down usually means traffic is not reaching a healthy backend because the pods, endpoints, ingress, load balancer, or network path are unhealthy or misconfigured.\n"
            "**Fix:** Check service endpoints, pod readiness, ingress or load balancer health, DNS resolution, recent deploy changes, and network policies.\n"
            "**Why this works:** A service is only available when the traffic path and the backing workloads are both healthy, so verifying that chain isolates the actual failure point.\n"
            "**Watch out for:** A quick pod restart can mask the issue. Also check rollout history and dependency failures before declaring the service recovered."
        ),
        "generic": (
            "**Root Cause:** This looks like an infrastructure or application failure that needs the failing component, error signal, and recent change context identified before choosing a durable fix.\n"
            "**Fix:** Start with the exact error, workload status, recent deployment changes, logs, events, and dependency health so the failing layer can be isolated quickly.\n"
            "**Why this works:** Troubleshooting becomes reliable when you narrow the fault domain first instead of changing multiple things at once.\n"
            "**Watch out for:** Avoid treating the first symptom as the root cause. Check whether the issue is coming from config, dependencies, capacity, or rollout changes."
        ),
    }

    answer = canned_answers.get(topic, canned_answers["generic"])
    confidence = "high" if topic != "generic" else "medium"

    return AnswerPlan(
        intent="troubleshooting",
        mode="deterministic",
        topic=topic,
        answer=answer,
        evidence={
            "topic": topic,
            "evidence_blocks": list(evidence.evidence_blocks or []),
            "sources": evidence.facts.get("sources", []),
        },
        entities=list(evidence.entities or []),
        confidence=confidence,
    )


def build_architecture_plan(route, evidence) -> AnswerPlan:
    entities = [entity for entity in (evidence.entities or []) if _should_keep_architecture_entity(entity, evidence.evidence_blocks)]
    evidence_blocks = list(evidence.evidence_blocks or [])
    topic = evidence.topic or route.topic or "external"
    response_mode = evidence.facts.get("response_mode", route.response_mode or "text")

    component_lines = []
    for entity in entities[:6]:
        component_lines.append(f"- **{entity}**: {_component_role(entity, evidence_blocks)}")

    request_flow = _pick_architecture_lines(
        evidence_blocks,
        entities,
        "route", "request", "front door", "gateway", "edge", "client", "service",
    )
    data_flow = _pick_architecture_lines(
        evidence_blocks,
        entities,
        "event", "stream", "publish", "consume", "cache", "store", "write", "read", "transport",
    )
    why_used = []
    if entities:
        if any(entity.lower() == "zuul" for entity in entities):
            why_used.append("- Zuul is used to keep routing, resiliency, and edge concerns concentrated at the front door.")
        if any(entity.lower() == "kafka" for entity in entities):
            why_used.append("- Kafka is used as the durable async transport so services can publish events without blocking client traffic.")
        if any(entity.lower() == "cassandra" for entity in entities):
            why_used.append("- Cassandra is used when the system needs a highly available store for large-scale serving data.")
        if any(entity.lower() == "evcache" for entity in entities):
            why_used.append("- EVCache is used to reduce read latency by serving hot data from memory.")
    if not why_used:
        why_used = [
            "- These components are used together to separate synchronous request handling from asynchronous data movement and storage.",
            "- The retrieved evidence shows routing, event transport, storage, and caching working as distinct responsibilities.",
        ]

    if not request_flow:
        if topic == "self_runtime":
            request_flow = [
                "Client traffic reaches ava-agent, which serves requests through Flask/Gunicorn on port 5443.",
                "During request handling, ava-agent consults OPA for policy decisions and Vault for secrets when needed.",
            ]
        else:
            request_flow = ["The retrieved evidence shows edge or client-facing services routing requests to backend services before asynchronous processing happens."]
    if not data_flow:
        if topic == "self_runtime":
            data_flow = [
                "ava-agent persists durable relational state in PostgreSQL.",
                "ava-agent keeps fast state and caching data in Redis and sends model inference requests to the Ollama host.",
            ]
        else:
            data_flow = ["The retrieved evidence shows events moving through streaming components into durable stores and caches for low-latency reads."]

    if response_mode == "diagram":
        if topic != "self_runtime" and any(term in (route.normalized_query or "").lower() for term in ("lifecycle", "devops lifecycle")):
            answer = _build_lifecycle_mermaid(route.normalized_query, entities)
        else:
            answer = _build_architecture_mermaid(entities, evidence_blocks, topic)
        confidence = "high" if topic == "self_runtime" or len(evidence_blocks) >= 2 else "medium"
    else:
        sections = [
            "**Components and Roles:**",
            *(component_lines or ["- The retrieved evidence did not expose enough named components to build a stronger component map."]),
            "",
            "**Request Flow:**",
            *[f"- {line}" for line in request_flow],
            "",
            "**Data Flow:**",
            *[f"- {line}" for line in data_flow],
            "",
            "**Why They Are Used:**",
            *why_used,
        ]
        answer = "\n".join(sections)
        confidence = "high" if topic == "self_runtime" or len(evidence_blocks) >= 3 else "medium"

    return AnswerPlan(
        intent="architecture",
        mode="deterministic",
        topic=topic,
        answer=answer,
        evidence={
            "topic": topic,
            "response_mode": response_mode,
            "evidence_blocks": evidence_blocks,
            "sources": evidence.facts.get("sources", []),
        },
        entities=entities,
        confidence=confidence,
    )


def build_follow_up_plan(route, evidence) -> AnswerPlan:
    last_topic = evidence.facts.get("last_topic", "")
    previous_topic = evidence.facts.get("previous_topic", "")
    last_summary = evidence.facts.get("last_summary", "") or "I don't have a strong summary for the latest answer yet."
    if not last_topic:
        answer = "I don't have enough recent conversation context to answer that follow-up reliably."
        confidence = "low"
    elif previous_topic:
        if previous_topic == last_topic:
            answer = (
                f"Your recent questions stayed on the same topic: {last_topic}.\n\n"
                f"The latest answer summary was: {last_summary}"
            )
        else:
            answer = (
                f"Your most recent topic was {last_topic}.\n\n"
                f"The topic before that was {previous_topic}.\n\n"
                f"They differ because the latest turn focused on {last_topic}, while the earlier turn focused on {previous_topic}.\n\n"
                f"Latest answer summary: {last_summary}"
            )
        confidence = "high"
    else:
        answer = (
            f"Your most recent previous question was about {last_topic}.\n\n"
            f"Latest answer summary: {last_summary}"
        )
        confidence = "medium"
    return AnswerPlan(
        intent="follow_up",
        mode="deterministic",
        topic="follow_up",
        answer=answer,
        evidence=evidence.facts,
        entities=list(evidence.entities or []),
        confidence=confidence,
    )


def build_comparison_plan(route, evidence) -> AnswerPlan:
    targets = list(evidence.facts.get("targets") or route.comparison_targets or [])
    collected = evidence.facts.get("collected") or {}
    if len(targets) < 2:
        answer = "I need two clear options to compare."
        return AnswerPlan(
            intent="comparison",
            mode="deterministic",
            topic="comparison",
            answer=answer,
            evidence=evidence.facts,
            confidence="low",
        )

    left, right = targets[0], targets[1]
    left_lines = collected.get(left.lower(), [])
    right_lines = collected.get(right.lower(), [])

    left_summary = _clean_explanation_line(left_lines[0]) if left_lines else f"{left} is one of the options you asked to compare."
    right_summary = _clean_explanation_line(right_lines[0]) if right_lines else f"{right} is the other option you asked to compare."

    if left.lower() == "readiness probe":
        left_summary = "A readiness probe decides whether a container should receive traffic."
    if right.lower() == "readiness probe":
        right_summary = "A readiness probe decides whether a container should receive traffic."
    if left.lower() == "liveness probe":
        left_summary = "A liveness probe decides whether Kubernetes should restart an unhealthy container."
    if right.lower() == "liveness probe":
        right_summary = "A liveness probe decides whether Kubernetes should restart an unhealthy container."

    choose_lines = []
    if left.lower() == "readiness probe":
        choose_lines.append(f"- Choose **{left}** when you want Kubernetes to stop sending traffic to a container that is not ready yet.")
    elif left_lines and not _looks_like_noise(left_lines[0]):
        choose_lines.append(f"- Choose **{left}** when you want {_clean_explanation_line(left_lines[0]).rstrip('.')}.")
    if right.lower() == "liveness probe":
        choose_lines.append(f"- Choose **{right}** when you want Kubernetes to restart a container that is unhealthy or stuck.")
    elif right_lines and not _looks_like_noise(right_lines[0]):
        choose_lines.append(f"- Choose **{right}** when you want {_clean_explanation_line(right_lines[0]).rstrip('.')}.")
    if not choose_lines:
        choose_lines = [
            f"- Choose **{left}** when its operational trade-offs fit your rollout or reliability needs better.",
            f"- Choose **{right}** when you want the other deployment or infrastructure trade-off.",
        ]

    answer = "\n".join([
        f"**{left}:** {left_summary}",
        f"**{right}:** {right_summary}",
        "",
        "**When to choose each:**",
        *choose_lines,
    ])
    confidence = "high" if left_lines or right_lines else "medium"
    return AnswerPlan(
        intent="comparison",
        mode="deterministic",
        topic="comparison",
        answer=answer,
        evidence=evidence.facts,
        entities=list(evidence.entities or []),
        confidence=confidence,
    )


def _definition_subject(query: str) -> str:
    cleaned = (query or "").strip().rstrip("?.! ")
    cleaned = re.sub(r"^(what is|what are|define|explain)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(a|an|the)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def build_definition_plan(route, evidence) -> AnswerPlan:
    subject = _definition_subject(route.normalized_query)
    lines = list(evidence.evidence_blocks or [])
    if lines:
        preferred_line = None
        for line in lines:
            cleaned = _clean_explanation_line(_first_sentence(line))
            lower = cleaned.lower()
            if _looks_like_noise(cleaned):
                continue
            if subject and subject.lower() in lower and (" is " in lower or " are " in lower):
                preferred_line = cleaned
                break
        if preferred_line is None:
            for line in lines:
                cleaned = _clean_explanation_line(_first_sentence(line))
                if not _looks_like_noise(cleaned):
                    preferred_line = cleaned
                    break
        opening = preferred_line or _clean_explanation_line(_first_sentence(lines[0]))
        details = []
        for line in lines[1:4]:
            sentence = _clean_explanation_line(_first_sentence(line))
            if _looks_like_noise(sentence):
                continue
            if sentence and sentence.lower() != opening.lower():
                details.append(f"- {sentence}")
        answer_lines = [opening]
        if details:
            answer_lines.extend(["", "Practical Details:", *details])
        answer = "\n".join(answer_lines)
        confidence = "high"
    else:
        answer = f"{subject or 'This concept'} is an infrastructure concept, but I don't have enough grounded detail yet to define it confidently."
        confidence = "low"
    return AnswerPlan(
        intent="definition",
        mode="deterministic",
        topic="definition",
        answer=answer,
        evidence={
            "subject": subject,
            "evidence_blocks": lines,
            "sources": evidence.facts.get("sources", []),
        },
        entities=list(evidence.entities or []),
        confidence=confidence,
    )
