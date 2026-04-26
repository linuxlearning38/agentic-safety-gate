"""
Capability router for AVA's natural-language operational requests.

This module provides a stable semantic-ish contract between user wording and
AVA's structured tools. It is intentionally deterministic and local: the LLM
does not own routing, policy, or execution. The legacy phrase extractors remain
as compatibility fallbacks, but new operational coverage should prefer this
catalog so AVA grows by capabilities rather than one-off route branches.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


@dataclass(frozen=True)
class Capability:
    capability_id: str
    tool_name: str
    risk: str
    description: str
    terms: tuple[str, ...]
    phrases: tuple[str, ...]
    required_args: tuple[str, ...] = ()
    clarification: str = ""


@dataclass(frozen=True)
class CapabilityMatch:
    capability_id: str
    tool_name: str
    tool_args: dict[str, Any]
    confidence: str
    score: float
    missing: tuple[str, ...]
    clarification: str
    source: str = "capability_router_v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_STOP_WORDS = {
    "a", "all", "an", "and", "any", "are", "ava", "can", "could", "do",
    "does", "for", "from", "give", "have", "how", "i", "in", "is", "it",
    "me", "my", "now", "of", "on", "please", "right", "show", "status",
    "tell", "the", "there", "this", "to", "we", "what", "which", "with",
    "you", "your",
}

_GENERIC_TARGETS = {
    "container", "containers", "daemon", "details", "health", "host",
    "machine", "node", "server", "service", "status", "system",
}

_SERVICE_STOP_WORDS = _STOP_WORDS | _GENERIC_TARGETS | {
    "check", "inspect", "look", "review", "running", "up",
}


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "container.list",
        "list_containers",
        "low",
        "List Docker/container runtime workloads.",
        ("container", "containers", "docker", "workload", "workloads", "running", "ps", "up"),
        (
            "docker ps", "show containers", "list containers", "running containers",
            "containers up", "container workloads", "docker workloads",
            "what containers are running", "which containers are up",
        ),
    ),
    Capability(
        "docker.status",
        "check_docker",
        "low",
        "Inspect Docker daemon/runtime status.",
        ("docker", "daemon", "runtime", "engine", "server", "status", "info", "healthy", "health"),
        (
            "check docker", "docker status", "docker info", "docker daemon",
            "docker runtime", "is docker running", "docker health",
        ),
    ),
    Capability(
        "host.ports",
        "check_listening_ports",
        "low",
        "Inspect listening ports and exposed sockets.",
        ("port", "ports", "listener", "listeners", "listening", "socket", "sockets", "exposed", "open"),
        (
            "listening ports", "open ports", "exposed ports", "who is listening",
            "what is exposed", "network listeners", "list sockets",
        ),
    ),
    Capability(
        "auth.events",
        "check_auth_events",
        "low",
        "Inspect authentication/login events.",
        ("auth", "authentication", "login", "logins", "failed", "failure", "failures", "ssh"),
        (
            "auth events", "login failures", "failed logins", "authentication failures",
            "ssh failures", "who tried to login",
        ),
    ),
    Capability(
        "security.suspicious_activity",
        "check_suspicious_activity",
        "low",
        "Hunt for suspicious local activity signals.",
        ("suspicious", "activity", "threat", "compromise", "intrusion", "malicious", "attack"),
        (
            "suspicious activity", "under threat", "am i compromised",
            "look for threats", "hunt suspicious", "security check",
        ),
    ),
    Capability(
        "host.verify",
        "verify_system",
        "low",
        "Run baseline local system verification.",
        ("verify", "verification", "baseline", "system", "health", "state", "checkup", "check"),
        (
            "verify my system", "system verification", "current system state",
            "health check", "system checkup", "check my system",
        ),
    ),
    Capability(
        "host.disk",
        "check_disk",
        "low",
        "Inspect disk usage.",
        ("disk", "space", "usage", "full", "storage", "filesystem"),
        ("disk usage", "disk space", "is disk full", "storage usage", "filesystem usage"),
    ),
    Capability(
        "host.memory",
        "check_memory",
        "low",
        "Inspect memory usage.",
        ("memory", "ram", "usage", "free", "swap"),
        ("memory usage", "ram usage", "free memory", "swap usage"),
    ),
    Capability(
        "service.failed",
        "check_failed_services",
        "low",
        "List failed system services.",
        ("failed", "failing", "broken", "services", "service", "unit", "units"),
        ("failed services", "failing services", "broken services", "systemd failures"),
    ),
    Capability(
        "package.updates",
        "check_updates",
        "low",
        "Check package/security update posture.",
        ("update", "updates", "patch", "patches", "patching", "upgrade", "upgrades", "security"),
        (
            "do i need updates", "check updates", "package updates",
            "security updates", "patch status", "need patching",
        ),
    ),
    Capability(
        "vulnerability.scan",
        "scan_host_vulnerabilities",
        "low",
        "Run bounded host/container vulnerability scan.",
        ("cve", "cves", "vulnerability", "vulnerabilities", "scan", "trivy", "security"),
        ("scan vulnerabilities", "run vulnerability scan", "scan cves", "trivy scan", "bounded vulnerability scan"),
    ),
    Capability(
        "host.risk",
        "assess_host_risk",
        "low",
        "Assess and prioritize the host's primary concern.",
        ("risk", "priority", "prioritize", "fix", "first", "investigate", "biggest", "important", "concern"),
        (
            "what should i fix first", "biggest risk", "highest risk",
            "what should i investigate", "primary concern", "prioritize this host",
        ),
    ),
    Capability(
        "service.inspect",
        "inspect_service",
        "low",
        "Inspect one named service.",
        ("service", "daemon", "running", "active", "health", "inspect", "status"),
        ("inspect service", "service health", "service status", "is running", "daemon status"),
        required_args=("service",),
        clarification="I can inspect a service, but I need the service name. Example: inspect service nginx.",
    ),
    Capability(
        "process.inspect",
        "inspect_process",
        "low",
        "Inspect one process by PID.",
        ("pid", "process", "details", "inspect", "owner", "command"),
        ("inspect pid", "pid details", "process details", "inspect process"),
        required_args=("pid",),
        clarification="I can inspect a process, but I need the PID. Example: inspect pid 4321.",
    ),
)


def _normalize(text: str) -> str:
    lowered = (text or "").lower()
    lowered = lowered.replace("postgresql", "postgres").replace("postgress", "postgres")
    lowered = re.sub(r"[?!.:,]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_.-]+", _normalize(text))
        if token and token not in _STOP_WORDS
    }


def _extract_pid(q: str) -> str | None:
    for pattern in (r"\bpid\s+(\d{1,8})\b", r"\bprocess\s+(\d{1,8})\b"):
        match = re.search(pattern, q)
        if match:
            return match.group(1)
    return None


def _extract_service(q: str) -> str | None:
    if any(phrase in q for phrase in ("docker daemon", "docker runtime", "docker engine")):
        return None

    patterns = (
        r"\binspect\s+service\s+([a-z0-9][a-z0-9._-]*)\b",
        r"\bservice\s+([a-z0-9][a-z0-9._-]*)\b",
        r"\b([a-z0-9][a-z0-9._-]*)\s+service\b",
        r"\bis\s+([a-z0-9][a-z0-9._-]*)\s+(?:running|active|healthy|up)\b",
        r"\b([a-z0-9][a-z0-9._-]*)\s+(?:daemon|service)\s+(?:status|health)\b",
        r"\b(?:status|health)\s+of\s+([a-z0-9][a-z0-9._-]*)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, q)
        if not match:
            continue
        candidate = match.group(1).strip(" .,:;")
        if candidate and candidate not in _SERVICE_STOP_WORDS:
            return candidate
    return None


def _args_for(capability: Capability, q: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if "pid" in capability.required_args:
        pid = _extract_pid(q)
        if pid:
            args["pid"] = pid
    if "service" in capability.required_args:
        service = _extract_service(q)
        if service:
            args["service"] = service
    return args


def _missing_args(capability: Capability, args: dict[str, Any]) -> tuple[str, ...]:
    return tuple(arg for arg in capability.required_args if not args.get(arg))


def _score(capability: Capability, q: str, query_tokens: set[str]) -> float:
    score = 0.0
    for phrase in capability.phrases:
        if phrase in q:
            score += 5.0 + min(len(phrase.split()), 4) * 0.25
    terms = set(capability.terms)
    overlap = query_tokens & terms
    score += len(overlap) * 1.8
    if capability.capability_id == "service.inspect" and _extract_service(q):
        score += 4.0
    if capability.capability_id == "process.inspect" and _extract_pid(q):
        score += 4.0
    return score


def _confidence(score: float, missing: tuple[str, ...]) -> str:
    if missing:
        return "high"
    if score >= 7.0:
        return "high"
    if score >= 5.0:
        return "medium"
    return "low"


def route_capability(query: str) -> CapabilityMatch | None:
    q = _normalize(query)
    if not q:
        return None

    # Stateful restart requests must use operational/approval routing,
    # not low-risk read-only service inspection.
    if re.search(r"\brestart\b", q) and (
        "docker service" in q
        or "docker daemon" in q
        or re.search(r"\brestart\b.*\bservice\b", q)
    ):
        return None

    query_tokens = _tokens(q)
    best: tuple[float, Capability, dict[str, Any], tuple[str, ...]] | None = None
    for capability in CAPABILITIES:
        args = _args_for(capability, q)
        missing = _missing_args(capability, args)
        score = _score(capability, q, query_tokens)
        if missing and score < 5.0:
            continue
        if not missing and score < 5.0:
            continue
        if best is None or score > best[0]:
            best = (score, capability, args, missing)

    if best is None:
        return None

    score, capability, args, missing = best
    confidence = _confidence(score, missing)
    if confidence == "low":
        return None

    return CapabilityMatch(
        capability_id=capability.capability_id,
        tool_name=capability.tool_name,
        tool_args=args,
        confidence=confidence,
        score=round(score, 2),
        missing=missing,
        clarification=capability.clarification if missing else "",
    )
