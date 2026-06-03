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


def _with_mermaid_init(diagram: str) -> str:
    text = (diagram or "").strip()
    if not text.startswith("```mermaid"):
        return diagram
    init = "%%{init: {'themeVariables': {'fontSize': '16px'}}}%%\n"
    return text.replace("```mermaid\n", f"```mermaid\n{init}", 1)


def _clean_explanation_line(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", cleaned)
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


def _first_clean_signal_line(lines: list[str]) -> str:
    for line in lines or []:
        cleaned = _clean_explanation_line(_first_sentence(line))
        if cleaned and not _looks_like_noise(cleaned):
            return cleaned
    if lines:
        return _clean_explanation_line(_first_sentence(lines[0]))
    return ""


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
        return _known_architecture_mermaid("ava_runtime")

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
    return _with_mermaid_init("\n".join(lines))


def _known_architecture_flow_key(normalized_query: str, entities: list[str]) -> str | None:
    q = (normalized_query or "").lower()
    entity_text = " ".join(entity.lower() for entity in entities or [])
    combined = f"{q} {entity_text}"
    if "ingress" in combined and ("kubernetes" in combined or "k8s" in combined or "request flow" in q):
        return "kubernetes_ingress"
    if "ava" in combined and ("kubernetes" in combined or "k8s" in combined) and any(
        term in q for term in ("flow", "architecture", "diagram")
    ):
        return "ava_kubernetes"
    if "ava" in combined and "linux" in combined and "provision" in combined and any(
        term in q for term in ("flow", "architecture", "diagram")
    ):
        return "ava_linux_provisioning"
    if "netflix" in combined and any(term in q for term in ("flow", "architecture", "diagram")):
        return "netflix_streaming"
    if (
        "devops" in combined
        and any(term in q for term in ("flow", "architecture", "diagram", "lifecycle"))
        and not any(term in combined for term in ("kubernetes", "k8s", "docker", "terraform", "ci/cd", "cicd", "jenkins", "pipeline"))
    ):
        return "devops_lifecycle"
    if (
        ("kubernetes" in combined or "k8s" in combined)
        and any(term in q for term in ("flow", "architecture", "diagram"))
        and "lifecycle" not in q
    ):
        return "kubernetes_runtime"
    if any(term in combined for term in ("ci/cd", "cicd", "jenkins", "pipeline")) and any(
        term in q for term in ("flow", "architecture", "diagram", "pipeline")
    ):
        return "cicd_pipeline"
    if "docker" in combined and any(term in q for term in ("flow", "architecture", "diagram")) and "lifecycle" not in q:
        return "docker_runtime"
    if "terraform" in combined and any(term in combined for term in ("plan", "apply", "state", "drift", "flow", "diagram")):
        return "terraform_workflow"
    if "ava" in combined and any(term in q for term in ("flow", "architecture", "diagram")):
        return "ava_runtime"
    return None


def _known_architecture_text(flow_key: str) -> str:
    templates = {
        "ava_runtime": (
            "**Components and Roles:**\n"
            "- **Browser/UI:** The user-facing chat surface at `https://localhost:5443`.\n"
            "- **ava-agent (Flask/Gunicorn):** The single serving contract. It classifies requests before producing an answer.\n"
            "- **AVA router/planner:** Chooses exact self/runtime, grounded DevOps knowledge, provisioning, guarded action, or Qwen reasoning.\n"
            "- **OPA + approval guard:** Enforces policy and approval before risky actions.\n"
            "- **Redis:** Fast state, runner heartbeat, provisioning queue, and Day-2 operation result signals.\n"
            "- **PostgreSQL / runtime data store:** Durable application state and persisted AVA records.\n"
            "- **Vault:** Secret-management dependency for sensitive runtime configuration.\n"
            "- **Ollama/Qwen:** Local model inference. Qwen reasons, but AVA owns truth and safety.\n"
            "- **Windows host runner:** Native runner that executes approved VirtualBox and SSH operations outside the container.\n"
            "- **VirtualBox + Ubuntu VMs:** Provider layer and managed servers created by Phase 9 provisioning.\n\n"
            "**Request Flow:**\n"
            "- User requests hit ava-agent first; AVA classifies the request before any answer is produced.\n"
            "- Knowledge questions use grounded retrieval and, when needed, Qwen through Ollama.\n"
            "- Provisioning requests create an approval, enqueue a runner job in Redis, then the Windows host runner creates/verifies the VM.\n"
            "- Day-2 operations reuse the runner to verify, snapshot, inspect logs, restart services, or manage VM power with approval where required.\n\n"
            "**Data Flow:**\n"
            "- Stored provisioning evidence is persisted as history; live verification must come from the host runner.\n"
            "- Redis carries fast queue/status signals; durable state lives in AVA's runtime stores.\n"
            "- Qwen receives only the context AVA chooses; it is not the owner of routing, policy, or runtime truth.\n\n"
            "**Failure Points:**\n"
            "- AVA container health, Redis/OPA/Postgres/Vault availability, Ollama reachability, runner heartbeat, VirtualBox state, NAT port conflicts, and VM bootstrap/network readiness.\n\n"
            "**Operational Checks:**\n"
            "- Check `/health`, container status, Redis/OPA/Postgres/Vault, Ollama tags, runner heartbeat, VirtualBox VM state, SSH reachability, and HTTP 200 from created servers."
        ),
        "ava_kubernetes": (
            "**Components and Roles:**\n"
            "- **Ingress/Gateway:** Entry point for external traffic.\n"
            "- **ava-service:** Stable Service endpoint for AVA pods.\n"
            "- **ava-agent Pods:** Runtime workers serving AVA requests.\n"
            "- **Redis/PostgreSQL:** State and cache backends.\n"
            "- **OPA/Vault/Ollama endpoints:** Policy, secrets, and model inference dependencies.\n\n"
            "**Request Flow:**\n"
            "- External traffic enters through ingress, routes to ava-service, then to ready ava-agent pods.\n"
            "- Pods call dependent services (OPA/Vault/Ollama/state stores) during request handling.\n\n"
            "**Data Flow:**\n"
            "- AVA runtime state flows between pods and data services through cluster networking.\n"
            "- Secrets and policy decisions are fetched on-demand for guarded operations.\n\n"
            "**Failure Points:**\n"
            "- Ingress routing, Service endpoints, pod readiness, dependency service availability, PVC health.\n\n"
            "**Operational Checks:**\n"
            "- Validate ingress events, service endpoints, pod readiness probes, and dependent service connectivity."
        ),
        "kubernetes_runtime": (
            "**Components and Roles:**\n"
            "- **Client:** Source of inbound requests.\n"
            "- **Ingress/Gateway:** Cluster edge routing.\n"
            "- **Kubernetes Service:** Stable backend discovery.\n"
            "- **Deployment/ReplicaSet/Pods:** Workload controller chain and runtime instances.\n"
            "- **Readiness Probes:** Endpoint eligibility guard.\n\n"
            "**Request Flow:**\n"
            "- Traffic enters ingress, is routed to Service, and then sent only to ready pod endpoints.\n"
            "- Deployment/ReplicaSet maintain desired pod count and rollout behavior.\n\n"
            "**Data Flow:**\n"
            "- Request payloads traverse ingress -> service -> pod and return through the reverse path.\n"
            "- Readiness state controls whether a pod participates in live service routing.\n\n"
            "**Failure Points:**\n"
            "- Ingress class/rules, service selectors/endpoints, probe failures, rollout drift, DNS/TLS issues.\n\n"
            "**Operational Checks:**\n"
            "- Inspect ingress/controller logs, service endpoints, pod readiness, rollout status, and events."
        ),
        "devops_lifecycle": (
            "**Lifecycle Stages:**\n"
            "- **Plan -> Code -> Build -> Test/Scan -> Package -> Deploy -> Observe -> Improve**\n\n"
            "**Components and Roles:**\n"
            "- **Planning/Backlog:** Defines desired change and risk.\n"
            "- **SCM + CI:** Builds and validates the change.\n"
            "- **Security/Quality Gates:** Enforces policy before release.\n"
            "- **Registry + Runtime Platform:** Delivers immutable artifacts.\n"
            "- **Observability Loop:** Feeds incidents and performance signals back into planning.\n\n"
            "**Request/Data Flow:**\n"
            "- Change intent becomes code, then artifact, then runtime deployment, then telemetry feedback.\n\n"
            "**Failure Points:**\n"
            "- Missing tests/gates, mutable artifacts, unsafe rollout, weak telemetry, no rollback discipline.\n\n"
            "**Operational Checks:**\n"
            "- Verify gate pass/fail evidence, deployment health, rollback readiness, and SLO-driven feedback loops."
        ),
        "netflix_streaming": (
            "**Components and Roles:**\n"
            "- **Zuul/API Gateway:** Entry/routing layer.\n"
            "- **Backend Services:** Domain service tier.\n"
            "- **Kafka:** Durable event transport.\n"
            "- **Samza/Mantis:** Stream processing and near-real-time computation.\n"
            "- **Cassandra:** Durable serving data store.\n"
            "- **EVCache:** Hot-path cache for low latency.\n\n"
            "**Request Flow:**\n"
            "- Client requests enter via gateway, fan into backend services, and may publish events to Kafka.\n\n"
            "**Data Flow:**\n"
            "- Streams are processed and persisted into serving stores; hot reads are accelerated through cache.\n\n"
            "**Failure Points:**\n"
            "- Gateway routing, stream lag, consumer health, storage saturation, cache miss storms.\n\n"
            "**Operational Checks:**\n"
            "- Track gateway errors/latency, Kafka lag, stream processor backlog, Cassandra health, EVCache hit ratio."
        ),
        "ava_linux_provisioning": (
            "**Status Note:**\n"
            "- Phase 9 chat-to-VM provisioning is active on `v2-development`, with Day-2 operations in v2.1.\n\n"
            "**Components:**\n"
            "- **Chat intent router -> approval gate -> Redis job queue -> Windows host runner -> VirtualBox adapter -> Ubuntu VM -> bootstrap/hardening -> verification/evidence**\n\n"
            "**Flow:**\n"
            "- AVA parses the web-server request, collects specs, and requires chat approval.\n"
            "- After approval, AVA queues a host-side runner job; the Windows runner creates the VirtualBox VM, injects cloud-init access, bootstraps nginx, applies baseline hardening, and verifies SSH/HTTP.\n"
            "- AVA reports PuTTY details and evidence only after the runner writes the result.\n\n"
            "**Day-2 Boundary:**\n"
            "- Later operations such as live verify, nginx logs, snapshots, restart, stop/start, and rollback go back through the same guarded host-runner path."
        ),
        "kubernetes_ingress": (
            "**Components and Roles:**\n"
            "- **Client/DNS:** Resolves the public hostname and sends HTTP or HTTPS traffic toward the cluster edge.\n"
            "- **Ingress Controller:** Watches Ingress objects and programs the real proxy or load balancer behavior.\n"
            "- **Ingress:** Holds host/path/TLS routing rules that map external requests to a Kubernetes Service.\n"
            "- **Service:** Selects backend Pods by label and exposes stable cluster networking for those Pods.\n"
            "- **Pods and Readiness:** Only ready Pods should receive traffic through Service endpoints.\n\n"
            "**Request Flow:**\n"
            "- Client DNS resolution points traffic to the load balancer or ingress controller.\n"
            "- The ingress controller evaluates the Ingress host/path/TLS rule and forwards the request to the target Service.\n"
            "- The Service sends traffic only to ready Pod endpoints; the response returns over the same path.\n\n"
            "**Data Flow:**\n"
            "- The HTTP request body flows from client to ingress controller, then to Service, then to a selected Pod.\n"
            "- Kubernetes endpoint readiness controls which Pods are eligible for live traffic.\n\n"
            "**Failure Points:**\n"
            "- DNS record, TLS certificate/secret, ingress class, controller health, host/path rule, Service selector, endpoints, readiness probes, or application port.\n\n"
            "**Operational Checks:**\n"
            "- Check DNS, Ingress status/events, controller logs, Service selectors/endpoints, Pod readiness, and recent rollout changes before changing traffic rules."
        ),
        "cicd_pipeline": (
            "**Pipeline Flow:**\n"
            "- Commit or merge triggers CI, CI builds the artifact, tests and scans it, pushes it to the registry, then deploys through the release controller.\n\n"
            "**Components and Roles:**\n"
            "- **Git Repository:** Source of truth for application and infrastructure changes.\n"
            "- **CI Runner:** Executes build, unit tests, integration tests, and security checks from a clean job environment.\n"
            "- **Build Artifact/Image:** Immutable output that should be versioned by digest or release identifier.\n"
            "- **Security Scan:** Checks dependencies, container image, IaC, and secrets before release.\n"
            "- **Registry:** Stores the approved artifact or image used by deployment.\n"
            "- **Deployment Controller:** Rolls the artifact into the target runtime and observes health.\n\n"
            "**Data Flow:**\n"
            "- Source code becomes a build artifact; metadata, test results, scan results, image digest, and deployment status move through the pipeline as release evidence.\n\n"
            "**Failure Points:**\n"
            "- Broken build, flaky tests, failed scan, registry authentication, mutable tags, deployment health checks, missing rollback, or observability gaps.\n\n"
            "**Operational Checks:**\n"
            "- Verify pipeline logs, test reports, scan output, artifact digest, deployment events, health probes, and rollback path before promoting the release."
        ),
        "docker_runtime": (
            "**Components and Roles:**\n"
            "- **Docker CLI/API:** Entry point where operators submit build, run, inspect, and lifecycle commands.\n"
            "- **Docker Engine (dockerd):** Control plane that manages images, containers, networks, and volumes on the host.\n"
            "- **Image Registry:** Source for pulling versioned images and destination for pushing built images.\n"
            "- **Container Runtime:** Executes isolated containers from images with configured resources and namespaces.\n"
            "- **Volumes/Networks:** Provide persistent data paths and service connectivity between containers and hosts.\n\n"
            "**Request Flow:**\n"
            "- Operator command reaches Docker Engine, which resolves image/runtime requirements and starts or updates containers.\n"
            "- Docker Engine validates requested resources, applies network/volume wiring, and orchestrates container lifecycle state.\n\n"
            "**Data Flow:**\n"
            "- Image layers are pulled from registry into local cache, then mounted as container filesystems.\n"
            "- Runtime logs and metrics flow from containers back to host observability and operational tooling.\n\n"
            "**Failure Points:**\n"
            "- Registry auth failures, image pull errors, port conflicts, volume mount issues, runtime permission denials, or host resource pressure.\n\n"
            "**Operational Checks:**\n"
            "- Verify daemon health, image pull status, container state, port bindings, volume mounts, and host CPU/memory/disk saturation."
        ),
        "terraform_workflow": (
            "**Components and Roles:**\n"
            "- **Terraform Configuration:** Desired infrastructure state written as code.\n"
            "- **Providers:** Plugins that talk to cloud or platform APIs.\n"
            "- **State Backend:** Tracks real resources Terraform manages and should be locked during updates.\n"
            "- **Plan:** Shows proposed create/update/delete operations before execution.\n"
            "- **Apply:** Executes the reviewed plan and updates state after successful changes.\n"
            "- **Drift Detection:** Compares declared state, recorded state, and real infrastructure behavior.\n\n"
            "**Request Flow:**\n"
            "- Operator runs init/validate, creates a plan, reviews the diff and policy checks, approves apply, then verifies the resulting infrastructure.\n\n"
            "**Data Flow:**\n"
            "- Configuration and variables feed the plan; provider API reads produce the diff; apply writes changes and records the new resource state.\n\n"
            "**Failure Points:**\n"
            "- Provider authentication, backend lock failure, stale state, destructive plan, policy violation, manual drift, or partial apply.\n\n"
            "**Operational Checks:**\n"
            "- Review plan output, state lock, backend health, drift report, provider permissions, and rollback/import strategy before applying infrastructure changes."
        ),
    }
    return templates[flow_key]


def _known_architecture_mermaid(flow_key: str) -> str:
    diagrams = {
        "ava_runtime": (
            "```mermaid\n"
            "graph LR\n"
            "    User[\"User / Browser\"] --> UI[\"AVA UI\\nhttps://localhost:5443\"]\n"
            "    UI --> Ava[\"ava-agent:5443\\nFlask/Gunicorn\"]\n"
            "    Ava --> Router[\"AVA Router + Planner\\nmode selection\"]\n"
            "    Router --> Knowledge[\"Grounded DevOps Knowledge\"]\n"
            "    Router --> Approval[\"Approval + Policy Guard\"]\n"
            "    Router --> Qwen[\"Ollama / Qwen\\nreasoning engine\"]\n"
            "    Ava --> Redis[\"Redis:6379\\nqueues + fast state\"]\n"
            "    Ava --> Postgres[\"PostgreSQL:5432\\ndurable state\"]\n"
            "    Ava --> OPA[\"OPA:8181\\npolicy\"]\n"
            "    Ava --> Vault[\"Vault:8200\\nsecrets\"]\n"
            "    Approval --> Redis\n"
            "    Redis --> Runner[\"Windows Host Runner\\nPowerShell/Python\"]\n"
            "    Runner --> VBox[\"VirtualBox\"]\n"
            "    VBox --> VM[\"Ubuntu VM\\nnginx/web_server\"]\n"
            "    Runner --> Evidence[\"Live evidence\\nSSH/HTTP/snapshot/logs\"]\n"
            "    Evidence --> Ava\n"
            "```"
        ),
        "ava_kubernetes": (
            "```mermaid\n"
            "graph LR\n"
            "    User[\"User\"] --> Ingress[\"Ingress / API Gateway\"]\n"
            "    Ingress --> Service[\"ava-service\"]\n"
            "    Service --> Pods[\"ava-agent Pods\"]\n"
            "    Config[\"ConfigMap / Secrets\"] --> Pods\n"
            "    Pods --> Redis[\"Redis\"]\n"
            "    Pods --> Postgres[\"PostgreSQL\"]\n"
            "    Pods --> OPA[\"OPA Service\"]\n"
            "    Pods --> Vault[\"Vault Service\"]\n"
            "    Pods --> Ollama[\"Ollama Endpoint\"]\n"
            "```"
        ),
        "kubernetes_runtime": (
            "```mermaid\n"
            "graph LR\n"
            "    Client[\"Client\"] --> Ingress[\"Ingress / Gateway\"]\n"
            "    Ingress --> Service[\"Kubernetes Service\"]\n"
            "    Service --> Endpoints[\"Ready Endpoints\"]\n"
            "    Endpoints --> Pods[\"Pods\"]\n"
            "    Deployment[\"Deployment\"] --> ReplicaSet[\"ReplicaSet\"]\n"
            "    ReplicaSet --> Pods\n"
            "    Readiness[\"Readiness Probes\"] --> Endpoints\n"
            "```"
        ),
        "devops_lifecycle": (
            "```mermaid\n"
            "graph LR\n"
            "    Plan[\"Plan\"] --> Code[\"Code\"]\n"
            "    Code --> Build[\"Build\"]\n"
            "    Build --> Test[\"Test / Security Scan\"]\n"
            "    Test --> Package[\"Package / Registry\"]\n"
            "    Package --> Deploy[\"Deploy\"]\n"
            "    Deploy --> Operate[\"Operate\"]\n"
            "    Operate --> Observe[\"Observe / Monitor\"]\n"
            "    Observe --> Improve[\"Improve / Backlog\"]\n"
            "    Improve --> Plan\n"
            "```"
        ),
        "netflix_streaming": (
            "```mermaid\n"
            "graph LR\n"
            "    Client[\"Client\"] --> Zuul[\"Zuul / API Gateway\"]\n"
            "    Zuul --> Services[\"Backend Services\"]\n"
            "    Services --> Kafka[\"Kafka\"]\n"
            "    Kafka --> Stream[\"Samza / Mantis\"]\n"
            "    Stream --> Cassandra[\"Cassandra\"]\n"
            "    Cassandra --> EVCache[\"EVCache\"]\n"
            "    EVCache --> Client\n"
            "```"
        ),
        "ava_linux_provisioning": (
            "```mermaid\n"
            "graph LR\n"
            "    User[\"User Prompt\"] --> Intent[\"AVA Intent Router\"]\n"
            "    Intent --> Approval[\"Approval Gate\"]\n"
            "    Approval --> Queue[\"Redis Provisioning Queue\"]\n"
            "    Queue --> Runner[\"Windows Host Runner\"]\n"
            "    Runner --> Adapter[\"VirtualBox Adapter\"]\n"
            "    Adapter --> VM[\"Ubuntu Linux VM\"]\n"
            "    VM --> CloudInit[\"Cloud-init Access\"]\n"
            "    CloudInit --> Bootstrap[\"nginx Bootstrap\"]\n"
            "    Bootstrap --> Harden[\"baseline_linux Hardening\"]\n"
            "    Harden --> Verify[\"SSH + HTTP 200 Verification\"]\n"
            "    Verify --> Evidence[\"Runner Evidence\"]\n"
            "    Evidence --> Chat[\"AVA Chat Status / PuTTY Details\"]\n"
            "```"
        ),
        "kubernetes_ingress": (
            "```mermaid\n"
            "graph LR\n"
            "    Client[\"Client\"] --> DNS[\"DNS\"]\n"
            "    DNS --> Controller[\"Ingress Controller / Load Balancer\"]\n"
            "    Controller --> Ingress[\"Ingress host/path/TLS rules\"]\n"
            "    Ingress --> Service[\"Kubernetes Service\"]\n"
            "    Service --> Endpoints[\"Ready Endpoints\"]\n"
            "    Endpoints --> Pods[\"Pods\"]\n"
            "    Readiness[\"Readiness Probes\"] --> Endpoints\n"
            "```"
        ),
        "cicd_pipeline": (
            "```mermaid\n"
            "graph LR\n"
            "    Git[\"Git Commit\"] --> Build[\"Build\"]\n"
            "    Build --> Test[\"Test\"]\n"
            "    Test --> Scan[\"Security Scan\"]\n"
            "    Scan --> Registry[\"Artifact / Image Registry\"]\n"
            "    Registry --> Deploy[\"Deploy\"]\n"
            "    Deploy --> Observe[\"Observe Health\"]\n"
            "    Observe --> Rollback[\"Rollback if unhealthy\"]\n"
            "```"
        ),
        "docker_runtime": (
            "```mermaid\n"
            "graph LR\n"
            "    User[\"Operator / CI\"] --> CLI[\"Docker CLI / API\"]\n"
            "    CLI --> Engine[\"Docker Engine (dockerd)\"]\n"
            "    Engine --> Registry[\"Image Registry\"]\n"
            "    Engine --> Cache[\"Local Image Cache\"]\n"
            "    Cache --> Runtime[\"Container Runtime\"]\n"
            "    Runtime --> Container[\"Running Containers\"]\n"
            "    Engine --> Network[\"Docker Networks\"]\n"
            "    Engine --> Volumes[\"Docker Volumes\"]\n"
            "```"
        ),
        "terraform_workflow": (
            "```mermaid\n"
            "graph LR\n"
            "    Config[\"Terraform Config\"] --> Init[\"Init / Validate\"]\n"
            "    Init --> Plan[\"Plan\"]\n"
            "    State[\"State Backend + Lock\"] --> Plan\n"
            "    Plan --> Review[\"Review / Policy Check\"]\n"
            "    Review --> Apply[\"Apply\"]\n"
            "    Apply --> State\n"
            "    State --> Drift[\"Drift Detection\"]\n"
            "```"
        ),
    }
    return _with_mermaid_init(diagrams[flow_key])


def _build_lifecycle_mermaid(normalized_query: str, entities: list[str]) -> str:
    q = (normalized_query or "").lower()
    if "kubernetes" in q or "docker" in q or "devops" in q or "lifecycle" in q:
        return _with_mermaid_init(
            (
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
    elif evidence.topic == "goal":
        answer = (
            "My goal is to be one local, approval-aware DevOps assistant with one consistent serving contract.\n"
            "- I explain DevOps topics from grounded knowledge when a factual answer is needed.\n"
            "- I use Qwen through Ollama as a reasoning engine, but AVA owns routing, policy, memory, and runtime truth.\n"
            "- I block destructive requests, ask approval for risky operations, and keep operational evidence separate from old history.\n"
            "- I can provision AVA-managed VirtualBox servers and help inspect or manage them through the Windows host runner.\n"
            "- I can describe my own container/runtime boundaries without pretending Qwen is the source of truth."
        )
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
            "**Confirm symptom:** Verify the last container state says `OOMKilled`, capture restart count, memory limit, request, node memory pressure, and the time window of the kill.\n"
            "**Inspect evidence:** Compare peak memory usage with the configured limit, check recent deploy/config changes, traffic spikes, heap/cache growth, and whether other pods on the node were under pressure.\n"
            "**Likely cause:** The workload exceeded its memory cgroup limit, but the reason may be undersized limits, a leak, abnormal traffic, JVM/runtime heap settings, or node pressure.\n"
            "**Low-risk fix:** Right-size request/limit only after evidence supports it, reduce memory-heavy config, tune heap/cache settings, or roll back the recent change if memory rose after deploy.\n"
            "**Unsafe shortcuts to avoid:** Do not blindly restart the pod or only raise limits without checking usage; that can hide leaks, shift pressure to the node, and cause repeat incidents."
        ),
        "crashloopbackoff": (
            "**Confirm symptom:** Verify the pod is repeatedly restarting, note the restart count, last termination reason, exit code, events, and whether the failure began after a rollout.\n"
            "**Inspect evidence:** Check current and previous container logs, pod events, entrypoint/command, env vars, ConfigMap/Secret mounts, image tag, dependency reachability, and probe timing.\n"
            "**Likely cause:** `CrashLoopBackOff` is a restart pattern, not the root cause; common causes are app startup failure, missing config, bad image/entrypoint, permission errors, dependency failure, or aggressive probes.\n"
            "**Low-risk fix:** Fix the failing startup condition first, roll back the recent bad release if evidence points there, correct missing config/secret mounts, or relax probe timing only when startup duration is the issue.\n"
            "**Unsafe shortcuts to avoid:** Do not delete/recreate pods as the fix; it hides evidence. Do not disable probes permanently or restart workloads repeatedly without reading previous logs."
        ),
        "imagepullbackoff": (
            "**Confirm symptom:** Identify whether the failure is `ImagePullBackOff` or `ErrImagePull`, note the exact image reference, registry host, and whether this affects one pod or many.\n"
            "**Inspect evidence:** Check pod events for the exact pull error (wrong tag, auth failure, registry unreachable), confirm the image name/tag exists in the registry, and review imagePullSecrets on the pod and service account.\n"
            "**Likely cause:** `ImagePullBackOff` usually comes from a misspelled image tag, missing or expired registry credential, a private registry with no imagePullSecret, or the registry itself being unreachable from the cluster.\n"
            "**Low-risk fix:** Correct the image reference or tag, update or re-create the imagePullSecret with valid credentials and link it to the service account, verify registry reachability from a node, then trigger a new rollout.\n"
            "**Unsafe shortcuts to avoid:** Do not set `imagePullPolicy: Never` to skip the pull without verifying the image exists locally on every node; it will silently fail on new nodes or after node replacements."
        ),
        "pending": (
            "**Confirm symptom:** Confirm the pod has been in `Pending` for more than a few seconds and note the output of `kubectl describe pod` events — particularly scheduler and binding messages.\n"
            "**Inspect evidence:** Check resource requests vs. node allocatable capacity, PVC binding status, node taints/tolerations, affinity/anti-affinity rules, admission webhook rejections, and any namespace resource quotas.\n"
            "**Likely cause:** `Pending` pods are most often caused by insufficient CPU/memory on available nodes, an unbound PVC, a taint with no matching toleration, failed admission, or cluster autoscaler lag on scale-up.\n"
            "**Low-risk fix:** Free up node capacity or adjust resource requests, fix the PVC StorageClass/binding, add the required toleration, resolve the admission webhook rejection, or wait for autoscaler to provision a node before retrying.\n"
            "**Unsafe shortcuts to avoid:** Do not remove resource requests or set very low values to force scheduling; that can cause OOMKilled or CPU throttling on the same or other pods sharing the node."
        ),
        "pod_network": (
            "**Confirm symptom:** Identify whether the failure is Pod-to-Pod, Pod-to-Service, Service-to-Pod, DNS lookup, ingress, or egress traffic.\n"
            "**Inspect evidence:** Check Pod IPs, Service selectors/endpoints, readiness state, DNS lookup from another pod, NetworkPolicy allows/denies, CNI plugin health, node routes, and recent network changes.\n"
            "**Likely cause:** Pod network failures usually come from endpoint mismatch, readiness exclusion, DNS/CoreDNS problems, NetworkPolicy, CNI/node routing, or the app listening on the wrong interface.\n"
            "**Low-risk fix:** Correct selectors/endpoints, restore readiness, adjust NetworkPolicy narrowly, fix DNS/CNI health, or roll back the recent network change after confirming the failing hop.\n"
            "**Unsafe shortcuts to avoid:** Do not restart random pods or open broad network policy access before isolating the hop; that can mask the real failure or weaken security."
        ),
        "dns_failure": (
            "**Confirm symptom:** Verify whether only one service name fails, all cluster DNS fails, or external DNS fails from pods.\n"
            "**Inspect evidence:** Compare direct IP connectivity with DNS lookup, check service/endpoints, namespace/search path, CoreDNS pod health, CoreDNS logs, recent ConfigMap changes, and node DNS reachability.\n"
            "**Likely cause:** Kubernetes DNS failures usually come from missing endpoints, wrong service/namespace name, CoreDNS outage/config drift, NetworkPolicy blocking DNS, or upstream resolver problems.\n"
            "**Low-risk fix:** Fix the service/endpoints or query name first, then repair CoreDNS/config/network policy only after evidence shows DNS infrastructure is the failing layer.\n"
            "**Unsafe shortcuts to avoid:** Do not blindly restart CoreDNS or change cluster DNS settings before proving the failure is DNS-wide; many DNS symptoms are actually endpoint or namespace mistakes."
        ),
        "tls_certificate": (
            "**Confirm symptom:** Capture the exact TLS error, affected host/SNI, certificate subject/SAN, issuer, expiry, and whether the failure occurs at ingress, service mesh, client, or upstream.\n"
            "**Inspect evidence:** Check certificate chain, hostname match, expiry window, Kubernetes TLS secret contents, ingress/controller events, cert-manager status, trust bundle, and recent rotation changes.\n"
            "**Likely cause:** TLS failures usually come from expired certificates, hostname/SAN mismatch, incomplete chain, stale Kubernetes secret, wrong ingress reference, or clients missing the issuing CA.\n"
            "**Low-risk fix:** Renew or rotate the certificate through the normal issuer path, update the referenced TLS secret, fix hostname/SAN mismatch, or restore the correct CA bundle; verify with a non-destructive handshake check.\n"
            "**Unsafe shortcuts to avoid:** Do not disable TLS verification, publish private keys, or replace ingress secrets blindly without confirming the chain and rollback path."
        ),
        "redis_incident": (
            "**Confirm symptom:** Identify whether Redis is showing latency, timeouts, memory pressure, evictions, replication lag, blocked clients, or role/failover changes.\n"
            "**Inspect evidence:** Check `INFO` sections for memory, clients, persistence, replication, CPU, evicted keys, rejected connections, blocked clients, slowlog entries, hot keys, network errors, and recent config or deploy changes.\n"
            "**Likely cause:** Redis incidents commonly come from maxmemory pressure, eviction churn, slow commands, hot keys, connection storms, persistence fork pressure, replica lag, or network instability.\n"
            "**Low-risk fix:** Reduce hot-key/load pressure, fix client pooling and timeouts, remove slow commands, tune maxmemory only after confirming memory evidence, and fail over only after replica health is verified.\n"
            "**Unsafe shortcuts to avoid:** Do not run FLUSHALL, disable persistence, kill Redis, or force failover before preserving evidence and confirming replica safety."
        ),
        "postgres_incident": (
            "**Confirm symptom:** Capture the exact error, latency window, affected database, lock/wait symptoms, connection count, replication state, disk/WAL pressure, and recent schema or deploy changes.\n"
            "**Inspect evidence:** Check active sessions, wait events, locks, slow queries, query plans, connection pool saturation, autovacuum state, table/index bloat, replication lag, disk usage, and WAL growth.\n"
            "**Likely cause:** PostgreSQL incidents often come from lock contention, a bad query plan, exhausted connections, disk or WAL pressure, vacuum lag, replica lag, or a migration holding locks.\n"
            "**Low-risk fix:** Cancel only the specific unsafe query when evidence supports it, reduce connection storms through the pooler, free disk/WAL pressure, fix the query/index after plan validation, or tune autovacuum with a rollback plan.\n"
            "**Unsafe shortcuts to avoid:** Do not drop or truncate data, kill PostgreSQL, run VACUUM FULL during peak traffic, or restart the database before preserving evidence and confirming impact."
        ),
        "terraform_drift": (
            "**Confirm symptom:** Determine whether drift appears in a refresh-only plan, a normal plan, a failed apply, or a resource changed manually outside Terraform.\n"
            "**Inspect evidence:** Review workspace, backend, state lock, provider credentials, recent manual console changes, imported resources, provider default changes, lifecycle `ignore_changes`, and the exact planned add/change/destroy actions.\n"
            "**Likely cause:** Terraform drift usually comes from manual changes, stale or wrong state, provider/API default changes, wrong workspace or variables, partial applies, or unmanaged resources that need import/adoption.\n"
            "**Low-risk fix:** Use a reviewed refresh-only plan to identify drift, import intentional resources, revert accidental manual changes, correct workspace/variables, and apply only after the plan is reviewed and approved.\n"
            "**Unsafe shortcuts to avoid:** Do not run blind apply or destroy, edit state by hand, delete state files, or remove locks unless the lock is proven stale and the team agrees."
        ),
        "service_mesh_traffic": (
            "**Confirm symptom:** Identify the failing source, destination, namespace, route, HTTP/gRPC status, mTLS mode, sidecar injection state, and whether traffic fails before or after Envoy.\n"
            "**Inspect evidence:** Check sidecar presence, Envoy routes/clusters/listeners, VirtualService, DestinationRule, Gateway, Service endpoints, AuthorizationPolicy, PeerAuthentication, certificates, and mesh control-plane health.\n"
            "**Likely cause:** Service mesh traffic failures usually come from route-match errors, subset/version mismatch, mTLS policy mismatch, missing sidecars, authorization denies, stale Envoy config, cert issues, or endpoint readiness problems.\n"
            "**Low-risk fix:** Correct the narrow route or subset rule, restore sidecar injection, align mTLS policy with the workload, roll back the mesh config change, and verify with a small canary request path.\n"
            "**Unsafe shortcuts to avoid:** Do not disable mTLS globally, add broad allow-all policies, delete mesh policies blindly, or restart the whole mesh before isolating the failing hop."
        ),
        "cicd_failure": (
            "**Confirm symptom:** Identify the failing pipeline, stage, job, commit, artifact/image tag, environment, error message, and whether the failure is build, test, scan, publish, or deploy.\n"
            "**Inspect evidence:** Review job logs, test reports, dependency/cache changes, secret and permission scope, scanner output, registry authentication, artifact immutability, deploy events, and rollback status.\n"
            "**Likely cause:** CI/CD failures commonly come from broken dependencies, stale caches, flaky or failing tests, missing secrets, expired credentials, registry auth, scan/policy gates, invalid image tags, or deployment health failures.\n"
            "**Low-risk fix:** Fix the failing stage, pin or restore the dependency, update scoped secrets, rebuild an immutable artifact, roll back a bad deploy, and retry only after the failure is identified as transient.\n"
            "**Unsafe shortcuts to avoid:** Do not disable tests or security scans, force-push over evidence, redeploy unknown artifacts, or bypass approvals just to make the pipeline green."
        ),
        "service_down": (
            "**Confirm symptom:** Identify the failing URL/service, affected users, error code, timeout behavior, and whether the failure is internal, ingress, or external.\n"
            "**Inspect evidence:** Check service endpoints, pod readiness, ingress/load balancer health, DNS resolution, recent deployments, dependency health, and NetworkPolicy.\n"
            "**Likely cause:** Traffic is usually not reaching a healthy backend because pods, endpoints, ingress, load balancer, DNS, dependencies, or the network path are unhealthy or misconfigured.\n"
            "**Low-risk fix:** Restore healthy endpoints, roll back the bad release, fix ingress/DNS/dependency configuration, or narrow the failing network rule after confirming the broken hop.\n"
            "**Unsafe shortcuts to avoid:** Do not keep restarting pods as recovery proof; verify endpoint health and request success after the change."
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
    known_flow = None if topic == "self_runtime" else _known_architecture_flow_key(route.normalized_query, entities)

    if topic == "self_runtime":
        answer = _known_architecture_mermaid("ava_runtime") if response_mode == "diagram" else _known_architecture_text("ava_runtime")
        return AnswerPlan(
            intent="architecture",
            mode="deterministic",
            topic="ava_runtime",
            answer=answer,
            evidence={
                "topic": "ava_runtime",
                "response_mode": response_mode,
                "evidence_blocks": evidence_blocks,
                "sources": evidence.facts.get("sources", []),
            },
            entities=entities,
            confidence="high",
        )

    if known_flow:
        answer = _known_architecture_mermaid(known_flow) if response_mode == "diagram" else _known_architecture_text(known_flow)
        return AnswerPlan(
            intent="architecture",
            mode="deterministic",
            topic=known_flow,
            answer=answer,
            evidence={
                "topic": known_flow,
                "response_mode": response_mode,
                "evidence_blocks": evidence_blocks,
                "sources": evidence.facts.get("sources", []),
            },
            entities=entities,
            confidence="high",
        )

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

    left_norm = left.lower().strip()
    right_norm = right.lower().strip()
    left_canonical = "readiness probe" if left_norm in {"readiness", "readiness probes"} else ("liveness probe" if left_norm in {"liveness", "liveness probes"} else left_norm)
    right_canonical = "readiness probe" if right_norm in {"readiness", "readiness probes"} else ("liveness probe" if right_norm in {"liveness", "liveness probes"} else right_norm)
    pair = {left_canonical, right_canonical}

    if pair in ({"blue-green", "canary"}, {"blue-green deployment", "canary deployment"}):
        answer = "\n".join([
            "**Blue-green deployment:** Run the old and new production environments in parallel, validate the new version, then switch traffic to it in one cutover.",
            "**Canary deployment:** Send a small percentage of live traffic to the new version first, watch health and error signals, then increase traffic gradually if it stays healthy.",
            "",
            "**When to choose each:**",
            "- Choose **blue-green deployment** when you want a fast cutover, simple rollback, and you can afford to keep two environments ready during the switch.",
            "- Choose **canary deployment** when you want to reduce rollout risk by exposing the new version to only a small slice of users before full release.",
            "",
            "**Trade-off:**",
            "- **Blue-green** is easier to reason about operationally, but it usually needs two full environments during the cutover window.",
            "- **Canary** gives better progressive risk control, but it needs stronger observability and rollout discipline.",
        ])
        return AnswerPlan(
            intent="comparison",
            mode="deterministic",
            topic="comparison",
            answer=answer,
            evidence=evidence.facts,
            entities=list(evidence.entities or []),
            confidence="high",
        )

    left_summary = _first_clean_signal_line(left_lines) if left_lines else f"{left} is one of the options you asked to compare."
    right_summary = _first_clean_signal_line(right_lines) if right_lines else f"{right} is the other option you asked to compare."

    if left_canonical == "readiness probe":
        left_summary = "A readiness probe decides whether a container should receive traffic."
    if right_canonical == "readiness probe":
        right_summary = "A readiness probe decides whether a container should receive traffic."
    if left_canonical == "liveness probe":
        left_summary = "A liveness probe decides whether Kubernetes should restart an unhealthy container."
    if right_canonical == "liveness probe":
        right_summary = "A liveness probe decides whether Kubernetes should restart an unhealthy container."

    choose_lines = []
    if left_canonical == "readiness probe":
        choose_lines.append(f"- Choose **{left}** when you want Kubernetes to stop sending traffic to a container that is not ready yet.")
    elif left_lines:
        chosen_left = _first_clean_signal_line(left_lines)
        if chosen_left and not _looks_like_noise(chosen_left):
            choose_lines.append(f"- Choose **{left}** when you want {chosen_left.rstrip('.')}.")
    if right_canonical == "liveness probe":
        choose_lines.append(f"- Choose **{right}** when you want Kubernetes to restart a container that is unhealthy or stuck.")
    elif right_lines:
        chosen_right = _first_clean_signal_line(right_lines)
        if chosen_right and not _looks_like_noise(chosen_right):
            choose_lines.append(f"- Choose **{right}** when you want {chosen_right.rstrip('.')}.")
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
