"""
control/self_healer.py
Phase 5C — Autonomous self-healing for AVA.

Decision ladder (heal method):
  confidence >= 0.85 AND risk LOW  → auto-execute (when dry_run=False)
  confidence 0.6 – 0.85            → approval_queue + audit
  confidence < 0.6                 → audit only

All executions go through the unified secure executor so OPA validates
every command before it runs.
"""

import logging
import re
import subprocess
from datetime import datetime, timezone

from control import database as db
from control.approval import add_request
from control.secure_executor import execute_tool_safe

logger = logging.getLogger("ava.self_healer")

# ─── Healing playbook ─────────────────────────────────────────────────────────

HEALING_PLAYBOOK: dict[str, dict] = {
    "pod_crash": {
        "command":     "kubectl rollout restart deployment/{name}",
        "risk_level":  "LOW",
        "rollback":    "kubectl rollout undo deployment/{name}",
        "description": "Restart crashed deployment",
    },
    "oom_killed": {
        "command":     "kubectl set resources deployment/{name} --limits=memory={new_limit}",
        "risk_level":  "MEDIUM",
        "rollback":    "kubectl set resources deployment/{name} --limits=memory={old_limit}",
        "description": "Increase memory limit",
    },
    "disk_full": {
        "command":     "find /var/log -name '*.log' -mtime +7 -delete",
        "risk_level":  "LOW",
        "rollback":    None,
        "description": "Clean old logs older than 7 days",
    },
    "high_cpu": {
        "command":     "kubectl scale deployment/{name} --replicas={new_replicas}",
        "risk_level":  "LOW",
        "rollback":    "kubectl scale deployment/{name} --replicas={old_replicas}",
        "description": "Scale up deployment",
    },
    "service_down": {
        "command":     "systemctl restart {service_name}",
        "risk_level":  "LOW",
        "rollback":    "systemctl stop {service_name}",
        "description": "Restart down service",
    },
    "image_pull_error": {
        "command":     "kubectl delete pod {pod_name} -n {namespace}",
        "risk_level":  "LOW",
        "rollback":    None,
        "description": "Delete pod to force re-pull",
    },
    "node_not_ready": {
        "command":     "kubectl describe node {node_name}",
        "risk_level":  "LOW",
        "rollback":    None,
        "description": "Diagnose node issue (read-only)",
    },
    "cert_expiry": {
        "command":     "certbot renew --dry-run",
        "risk_level":  "LOW",
        "rollback":    None,
        "description": "Check cert renewal",
    },
}

# ─── Diagnostic playbook ─────────────────────────────────────────────────────

DIAGNOSTIC_PLAYBOOK: dict[str, dict] = {
    "pod_crash": {
        "possible_causes": [
            "Application error or exception",
            "Missing ConfigMap or Secret",
            "OOMKilled (memory limit too low)",
            "Image pull error",
            "Liveness probe failing",
        ],
        "diagnostic_commands": [
            "kubectl describe pod {pod_name} -n {namespace}",
            "kubectl logs {pod_name} -n {namespace} --previous",
            "kubectl get events -n {namespace} --sort-by=.lastTimestamp",
        ],
        "needs_more_info": True,
    },
    "oom_killed": {
        "possible_causes": [
            "Memory limit set too low",
            "Memory leak in application",
            "Sudden traffic spike",
        ],
        "diagnostic_commands": [
            "kubectl describe pod {pod_name} -n {namespace}",
            "kubectl top pod {pod_name} -n {namespace}",
        ],
        "needs_more_info": False,
    },
    "disk_full": {
        "possible_causes": [
            "Log files accumulation",
            "Container image layers",
            "Application writing too much data",
        ],
        "diagnostic_commands": [
            "df -h",
            "du -sh /var/log/*",
            "docker system df",
        ],
        "needs_more_info": False,
    },
    "high_cpu": {
        "possible_causes": [
            "Traffic spike",
            "Infinite loop in application",
            "Missing resource limits",
        ],
        "diagnostic_commands": [
            "kubectl top pods -n {namespace}",
            "kubectl describe node {node_name}",
        ],
        "needs_more_info": True,
    },
    "service_down": {
        "possible_causes": [
            "All pods crashed",
            "Service selector mismatch",
            "Network policy blocking traffic",
        ],
        "diagnostic_commands": [
            "kubectl get pods -n {namespace}",
            "kubectl describe service {service_name}",
            "kubectl get endpoints {service_name}",
        ],
        "needs_more_info": True,
    },
    "node_not_ready": {
        "possible_causes": [
            "Kubelet not running",
            "Node resource pressure",
            "Network connectivity issue",
        ],
        "diagnostic_commands": [
            "kubectl describe node {node_name}",
            "kubectl get events --field-selector involvedObject.name={node_name}",
        ],
        "needs_more_info": True,
    },
    "image_pull_error": {
        "possible_causes": [
            "Wrong image name or tag",
            "Registry authentication failed",
            "Network issue reaching registry",
        ],
        "diagnostic_commands": [
            "kubectl describe pod {pod_name} -n {namespace}",
            "kubectl get events -n {namespace}",
        ],
        "needs_more_info": False,
    },
    "cert_expiry": {
        "possible_causes": [
            "Certificate not renewed",
            "cert-manager not running",
            "Manual cert not replaced",
        ],
        "diagnostic_commands": [
            "openssl s_client -connect {host}:443 -servername {host} 2>/dev/null | openssl x509 -noout -dates",
            "kubectl get certificate -A",
        ],
        "needs_more_info": False,
    },
}

# ─── Keyword → issue_type classification ─────────────────────────────────────

_CLASSIFIERS: list[tuple[list[str], str, str]] = [
    # keywords, issue_type, severity
    (["oomkilled", "oom killed", "out of memory", "memory limit"],   "oom_killed",       "HIGH"),
    (["crashloopbackoff", "crash loop", "crashloop", "restarting"],  "pod_crash",        "HIGH"),
    (["imagepullbackoff", "image pull", "errimagepull"],             "image_pull_error", "MEDIUM"),
    (["notready", "not ready", "node not ready"],                    "node_not_ready",   "HIGH"),
    (["disk full", "no space left", "disk pressure", "diskpressure",
      "filesystem full", "disk usage", "% full", "% usage", "95%"],  "disk_full",        "HIGH"),
    (["high cpu", "cpu > ", "cpu usage", "cpu pressure"],            "high_cpu",         "MEDIUM"),
    (["service down", "service failed", "service is down", "inactive", "failed to start",
      "systemctl"],                                                   "service_down",     "HIGH"),
    (["certificate", "cert expir", "tls expir", "ssl expir"],        "cert_expiry",      "MEDIUM"),
]

# ─── Entity extraction helpers ────────────────────────────────────────────────

_ENTITY_SKIP = {
    "the", "a", "an", "my", "our", "this", "that", "pod", "deployment",
    "in", "is", "are", "was", "were", "at", "on", "of", "to", "for",
    "not", "has", "have", "its", "it", "be", "been",
}

def _extract_entities(message: str) -> dict:
    """Extract pod, deployment, namespace, node, service names from alert message."""
    entities: dict[str, str] = {
        "name":         "unknown",
        "pod_name":     "unknown",
        "deployment":   "unknown",
        "namespace":    "default",
        "node_name":    "unknown",
        "service_name": "unknown",
        "new_limit":    "512Mi",
        "old_limit":    "256Mi",
        "new_replicas": "3",
        "old_replicas": "1",
    }

    # Pod / deployment name — ordered from most to least specific
    pod_patterns = [
        r'pod[s]?\s+([a-zA-Z0-9][-a-zA-Z0-9]*)',
        r'([a-zA-Z0-9][-a-zA-Z0-9]*)\s+pod',
        r'deployment[/\s]+([a-zA-Z0-9][-a-zA-Z0-9]*)',
        r'([a-zA-Z0-9][-a-zA-Z0-9]*)-deployment',
        r'([a-zA-Z0-9][-a-zA-Z0-9]*)\s+is\s+in',
        r'([a-zA-Z0-9][-a-zA-Z0-9]*)\s+container',
        # fallback: any hyphenated k8s-style name
        r'\b([\w][\w\-]{2,}(?:-[\w]+)+)\b',
    ]
    for pattern in pod_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            name = match.group(1).lower()
            if name not in _ENTITY_SKIP:
                entities["pod_name"]     = name
                entities["deployment"]   = name
                entities["name"]         = name   # playbook templates use {name}
                entities["service_name"] = name
                break

    # Namespace
    ns_patterns = [
        r'namespace[s]?\s+([a-zA-Z0-9][-a-zA-Z0-9]*)',
        r'namespace[=:]\s*([a-zA-Z0-9][-a-zA-Z0-9]*)',
        r'in\s+(production|staging|default|kube-system|monitoring)\b',
    ]
    for pat in ns_patterns:
        ns_match = re.search(pat, message, re.IGNORECASE)
        if ns_match:
            entities["namespace"] = ns_match.group(1).lower()
            break
    if "kube-system" in message.lower():
        entities["namespace"] = "kube-system"

    # Node name
    node_match = re.search(r'node[s]?\s+([a-zA-Z0-9][-a-zA-Z0-9]*)', message, re.IGNORECASE)
    if not node_match:
        node_match = re.search(r'\bnode[=:]\s*(\S+)', message, re.IGNORECASE)
    if node_match:
        entities["node_name"] = node_match.group(1).lower().strip("\"',")

    # Service name (explicit override)
    svc_match = re.search(r'service[s]?\s+([a-zA-Z0-9][-a-zA-Z0-9]*)', message, re.IGNORECASE)
    if not svc_match:
        svc_match = re.search(r'\bservice[=:]\s*(\S+)', message, re.IGNORECASE)
    if svc_match:
        entities["service_name"] = svc_match.group(1).lower().strip("\"',")

    # Memory hints  (e.g. "512Mi", "2GB")
    mem_match = re.search(r'(\d+)\s*(Mi|Gi|MB|GB)', message, re.IGNORECASE)
    if mem_match:
        val  = int(mem_match.group(1))
        unit = mem_match.group(2).replace("MB", "Mi").replace("GB", "Gi")
        entities["old_limit"] = f"{val}{unit}"
        entities["new_limit"] = f"{val * 2}{unit}"

    return entities


class SelfHealer:
    """Autonomous self-healing engine."""

    # ── 1. classify ──────────────────────────────────────────────────────────

    def detect_issue(self, source: str, message: str) -> dict:
        """
        Classify an incoming alert into a structured issue dict.

        Returns:
            {issue_type, severity, suggested_action, confidence, entities}
        """
        m = message.lower()
        issue_type = "unknown"
        severity   = "LOW"
        confidence = 0.4

        for keywords, itype, isev in _CLASSIFIERS:
            hits = sum(1 for kw in keywords if kw in m)
            if hits:
                # Confidence scales with number of keyword hits
                score = min(0.95, 0.6 + hits * 0.1)
                if score > confidence:
                    confidence  = score
                    issue_type  = itype
                    severity    = isev

        playbook_entry = HEALING_PLAYBOOK.get(issue_type, {})
        entities       = _extract_entities(message)

        result = {
            "issue_type":       issue_type,
            "severity":         severity,
            "suggested_action": playbook_entry.get("description", "No action defined"),
            "confidence":       round(confidence, 3),
            "source":           source,
            "message":          message,
            "entities":         entities,
        }
        logger.info(
            f"[SelfHealer] detect_issue: type={issue_type} sev={severity} "
            f"conf={confidence:.2f} src={source}"
        )
        return result

    # ── 2. lookup ─────────────────────────────────────────────────────────────

    def get_healing_action(self, issue_type: str) -> dict:
        """Return the raw playbook entry for an issue type."""
        return HEALING_PLAYBOOK.get(issue_type, {})

    # ── 3. heal ───────────────────────────────────────────────────────────────

    def heal(self, issue: dict, dry_run: bool = True) -> dict:
        """
        Execute (or simulate) a healing action for the given issue dict.

        Returns:
            {action_taken, command_used, result, risk_level, timestamp}
        """
        issue_type = issue.get("issue_type", "unknown")
        confidence = float(issue.get("confidence", 0.0))
        entities   = issue.get("entities", {})
        ts         = datetime.now(timezone.utc).isoformat()

        playbook   = HEALING_PLAYBOOK.get(issue_type)
        if not playbook:
            return {
                "action_taken": "no_playbook",
                "command_used": None,
                "result":       f"No playbook entry for issue_type='{issue_type}'",
                "risk_level":   None,
                "timestamp":    ts,
            }

        risk_level = playbook["risk_level"]

        # Build command — fill template variables from entities via string replace
        # (avoids KeyError when entity keys don't exactly match template placeholders)
        cmd      = playbook["command"]
        rollback = playbook.get("rollback") or ""
        for key, val in entities.items():
            placeholder = "{" + key + "}"
            cmd      = cmd.replace(placeholder, val)
            if rollback:
                rollback = rollback.replace(placeholder, val)

        # ── Decision ladder ──────────────────────────────────────────────────
        if confidence >= 0.85 and risk_level == "LOW":
            if dry_run:
                action_taken = "dry_run"
                exec_result  = f"[DRY RUN] Would execute: {cmd}"
            else:
                action_taken = "auto_executed"
                exec_result  = self._execute(cmd, issue_type)
        elif confidence >= 0.6:
            action_taken = "queued_for_approval"
            exec_result  = f"Queued for approval — confidence={confidence:.2f} risk={risk_level}"
            try:
                add_request(
                    cmd,
                    f"self_heal:{issue_type}",
                    risk=(risk_level or "").lower(),
                    mode="command",
                    approval_key=cmd,
                    metadata={"source": "self_healer", "issue_type": issue_type},
                )
            except Exception as _e:
                logger.warning(f"[SelfHealer] approval queue write failed: {_e}")
        else:
            action_taken = "incident_logged"
            exec_result  = f"Low confidence ({confidence:.2f}) — incident logged only"

        # ── Audit every decision ──────────────────────────────────────────────
        try:
            db.add_audit(
                event_type=f"self_heal_{action_taken}",
                details=(
                    f"issue={issue_type} | risk={risk_level} | "
                    f"conf={confidence:.2f} | cmd={cmd[:200]} | "
                    f"dry_run={dry_run} | result={str(exec_result)[:200]}"
                ),
                user="self_healer",
            )
        except Exception as _e:
            logger.warning(f"[SelfHealer] audit write failed: {_e}")

        # Verify healing only when a command actually ran
        if action_taken == "auto_executed":
            verification = self.verify_healing(
                issue_type, entities, wait_seconds=5
            )
        else:
            verification = {"resolved": None, "status": "not_executed", "details": ""}

        diag = DIAGNOSTIC_PLAYBOOK.get(issue_type, {})
        out = {
            "action_taken":        action_taken,
            "command_used":        cmd,
            "result":              exec_result,
            "risk_level":          risk_level,
            "possible_causes":     diag.get("possible_causes", []),
            "diagnostic_commands": diag.get("diagnostic_commands", []),
            "needs_more_info":     diag.get("needs_more_info", False),
            "verification":        verification,
            "timestamp":           ts,
        }
        logger.info(f"[SelfHealer] heal: {action_taken} | cmd={cmd[:80]}")
        return out

    # ── 4. history ────────────────────────────────────────────────────────────

    def get_healing_history(self, n: int = 10) -> list:
        """Return last n self-heal audit entries."""
        try:
            conn = db._get_conn()
            rows = conn.execute(
                """SELECT timestamp, event_type, details, user
                   FROM audit_log
                   WHERE event_type LIKE 'self_heal_%'
                      OR event_type LIKE 'monitor_%'
                   ORDER BY id DESC
                   LIMIT ?""",
                (n,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[SelfHealer] history query failed: {e}")
            return []

    # ── 5. verify ─────────────────────────────────────────────────────────────

    def verify_healing(self, issue_type: str, entities: dict, wait_seconds: int = 10) -> dict:
        """
        After executing a fix, wait and verify if issue is resolved.
        Returns: {resolved: bool|None, status: str, details: str}
        """
        import time
        time.sleep(wait_seconds)

        result: dict = {"resolved": False, "status": "unknown", "details": ""}

        try:
            if issue_type == "pod_crash":
                cmd = [
                    "kubectl", "get", "deployment",
                    entities.get("deployment", "unknown"),
                    "-n", entities.get("namespace", "default"),
                    "-o", "jsonpath={.status.readyReplicas}",
                ]
                out = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10, shell=False
                )
                if out.returncode == 0 and out.stdout.strip().isdigit():
                    ready = int(out.stdout.strip())
                    result["resolved"] = ready > 0
                    result["status"]   = "healthy" if ready > 0 else "still_failing"
                    result["details"]  = f"Ready replicas: {ready}"
                else:
                    result["status"]   = "kubectl_unavailable"
                    result["details"]  = "Cannot verify — kubectl not in container"
                    result["resolved"] = None

            elif issue_type == "disk_full":
                out = subprocess.run(
                    ["df", "-h", "/var/log"],
                    capture_output=True, text=True, timeout=5, shell=False,
                )
                result["resolved"] = out.returncode == 0
                result["status"]   = "cleaned" if out.returncode == 0 else "failed"
                result["details"]  = out.stdout[:200] if out.stdout else ""

            elif issue_type == "service_down":
                out = subprocess.run(
                    ["systemctl", "is-active", entities.get("service_name", "unknown")],
                    capture_output=True, text=True, timeout=5, shell=False,
                )
                result["resolved"] = "active" in out.stdout
                result["status"]   = out.stdout.strip()

            else:
                result["status"]   = "no_verifier"
                result["details"]  = f"No verifier for {issue_type} yet"
                result["resolved"] = None

        except Exception as e:
            result["status"]   = "verify_error"
            result["details"]  = str(e)
            result["resolved"] = None

        return result

    # ── internal ──────────────────────────────────────────────────────────────

    def _execute(self, cmd: str, context_label: str = "") -> str:
        """
        Run cmd through OPA-validated secure executor.
        Returns a human-readable result string.
        """
        try:
            result = execute_tool_safe(
                "raw_command",
                {"command": cmd},
                query=f"self_heal:{context_label}",
                source="healer",
            )
            status = result.get("status")
            if status == "success":
                stdout = (result.get("output") or "").strip()
                return stdout[:400] if stdout else "(executed, no output)"
            elif status == "blocked":
                return f"BLOCKED by OPA: {result.get('reason', 'policy violation')}"
            elif status == "approval_required":
                aid = result.get("approval_id", "?")
                return f"approval_required (id={aid}): {result.get('reason', '')}"
            else:
                return f"Unexpected executor status: {status}"
        except Exception as e:
            logger.error(f"[SelfHealer] _execute error: {e}")
            return f"Execution error: {e}"


# Module-level singleton — imported by web_agent and monitor
healer = SelfHealer()
