"""
control/self_healer.py
Phase 5C — Autonomous self-healing for AVA.

Decision ladder (heal method):
  confidence >= 0.85 AND risk LOW  → auto-execute (when dry_run=False)
  confidence 0.6 – 0.85            → approval_queue + audit
  confidence < 0.6                 → audit only

All executions go through execute_command_secure() so OPA validates
every command before it runs.
"""

import logging
import re
from datetime import datetime, timezone

from control import database as db
from control.secure_executor import execute_command_secure

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

# ─── Keyword → issue_type classification ─────────────────────────────────────

_CLASSIFIERS: list[tuple[list[str], str, str]] = [
    # keywords, issue_type, severity
    (["oomkilled", "oom killed", "out of memory", "memory limit"],   "oom_killed",       "HIGH"),
    (["crashloopbackoff", "crash loop", "crashloop", "restarting"],  "pod_crash",        "HIGH"),
    (["imagepullbackoff", "image pull", "errimagepull"],             "image_pull_error", "MEDIUM"),
    (["notready", "not ready", "node not ready"],                    "node_not_ready",   "HIGH"),
    (["disk full", "no space left", "disk pressure", "diskpressure",
      "filesystem full", "df -h"],                                   "disk_full",        "HIGH"),
    (["high cpu", "cpu > ", "cpu usage", "cpu pressure"],            "high_cpu",         "MEDIUM"),
    (["service down", "service failed", "inactive", "failed to start",
      "systemctl"],                                                   "service_down",     "HIGH"),
    (["certificate", "cert expir", "tls expir", "ssl expir"],        "cert_expiry",      "MEDIUM"),
]

# ─── Entity extraction helpers ────────────────────────────────────────────────

def _extract_entities(message: str) -> dict:
    """Best-effort extraction of pod/deployment/node names from alert text."""
    m = message.lower()
    entities: dict[str, str] = {
        "name":         "unknown",
        "namespace":    "default",
        "pod_name":     "unknown",
        "node_name":    "unknown",
        "service_name": "unknown",
        "new_limit":    "512Mi",
        "old_limit":    "256Mi",
        "new_replicas": "3",
        "old_replicas": "1",
    }

    # Pod / deployment name — grab token that looks like a k8s resource
    pod_match = re.search(
        r"\b([\w][\w\-]{2,}(?:-[\w]+)+)\b",  # hyphenated k8s names
        message
    )
    if pod_match:
        raw = pod_match.group(1)
        entities["pod_name"] = raw
        # Strip trailing random pod suffix (last 2 hyphen-segments if long hash)
        parts = raw.split("-")
        if len(parts) > 2 and len(parts[-1]) >= 5:
            entities["name"] = "-".join(parts[:-2]) or raw
        elif len(parts) > 1 and len(parts[-1]) >= 5:
            entities["name"] = "-".join(parts[:-1]) or raw
        else:
            entities["name"] = raw

    # Namespace
    ns_match = re.search(r"\bnamespace[=: ]+(\S+)", m)
    if ns_match:
        entities["namespace"] = ns_match.group(1).strip("\"',")

    # Node name
    node_match = re.search(r"\bnode[=: ]+(\S+)", m)
    if node_match:
        entities["node_name"] = node_match.group(1).strip("\"',")

    # Service name
    svc_match = re.search(r"\bservice[=: ]+(\S+)", m)
    if svc_match:
        entities["service_name"] = svc_match.group(1).strip("\"',")

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

        # Build command — fill template variables from entities
        try:
            cmd = playbook["command"].format(**entities)
        except KeyError as exc:
            cmd = playbook["command"]   # leave unfilled placeholder if entity missing
            logger.warning(f"[SelfHealer] Template key missing: {exc} — using raw command")

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
                db.add_approval(command=cmd, risk_level=risk_level)
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

        out = {
            "action_taken": action_taken,
            "command_used": cmd,
            "result":       exec_result,
            "risk_level":   risk_level,
            "timestamp":    ts,
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

    # ── internal ──────────────────────────────────────────────────────────────

    def _execute(self, cmd: str, context_label: str = "") -> str:
        """
        Run cmd through OPA-validated secure executor.
        Returns a human-readable result string.
        """
        try:
            result = execute_command_secure(cmd, query=f"self_heal:{context_label}")
            status = result.get("status")
            if status == "executed":
                out = result.get("output", {})
                stdout = (out.get("stdout") or "").strip()
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
