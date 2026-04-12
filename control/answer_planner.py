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
