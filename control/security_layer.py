# control/security_layer.py
# AgentGuard - Security Layer for AI Agents
# Integrates with Phase 1 Control System

from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path

from control.runtime_paths import get_runtime_path

# ============= RISK CLASSIFICATION =============

RISK_PATTERNS = {
    "critical": {
        "patterns": [
            "rm -rf /",
            "dd if=/dev/zero",
            "mkfs",
            "> /dev/sd",
            "curl * | bash",
            "wget * | sh",
            "chmod 777",
            "chmod -R 777"
        ],
        "blast_radius": "system_wide",
        "description": "Can destroy system or data"
    },
    "high": {
        "patterns": [
            "systemctl stop",
            "systemctl disable",
            "docker rm",
            "docker rmi",
            "userdel",
            "groupdel",
            "iptables -F",
            "ufw disable"
        ],
        "blast_radius": "service_disruption",
        "description": "Can disrupt critical services"
    },
    "medium": {
        "patterns": [
            "systemctl restart",
            "docker restart",
            "systemctl reload",
            "apt-get install",
            "yum install",
            "pip install"
        ],
        "blast_radius": "service_restart",
        "description": "Temporary service interruption"
    },
    "low": {
        "patterns": [
            "systemctl status",
            "docker ps",
            "df -h",
            "free -h",
            "ps aux",
            "ls",
            "cat",
            "grep",
            "git status"
        ],
        "blast_radius": "read_only",
        "description": "Information gathering only"
    }
}

def classify_command_risk(cmd):
    """
    Classify command risk level
    Returns: dict with risk, blast_radius, reasoning
    """
    cmd_lower = cmd.lower().strip()
    
    for risk_level, config in RISK_PATTERNS.items():
        for pattern in config["patterns"]:
            if pattern in cmd_lower or cmd_lower.startswith(pattern.split()[0]):
                return {
                    "risk": risk_level,
                    "blast_radius": config["blast_radius"],
                    "description": config["description"],
                    "matched_pattern": pattern
                }
    
    # Unknown command - treat as medium risk
    return {
        "risk": "medium",
        "blast_radius": "unknown",
        "description": "Unknown command - requires review",
        "matched_pattern": None
    }


# ============= THREAT PATTERN DETECTION =============

THREAT_PATTERNS = {
    "credential_access": {
        "indicators": [
            "/etc/passwd",
            "/etc/shadow",
            "~/.ssh",
            "id_rsa",
            ".aws/credentials",
            "grep password",
            "cat password",
            ".env"
        ],
        "severity": "high",
        "description": "Attempting to access credentials"
    },
    "privilege_escalation": {
        "indicators": [
            "sudo",
            "su -",
            "chmod +s",
            "usermod -aG sudo",
            "visudo"
        ],
        "severity": "high",
        "description": "Attempting privilege escalation"
    },
    "data_exfiltration": {
        "indicators": [
            "curl http",
            "wget http",
            "scp",
            "rsync",
            "> /dev/tcp",
            "nc -l",
            "netcat"
        ],
        "severity": "medium",
        "description": "Potential data transfer"
    },
    "persistence": {
        "indicators": [
            "crontab",
            "/etc/cron",
            ".bashrc",
            ".bash_profile",
            "systemctl enable"
        ],
        "severity": "medium",
        "description": "Modifying persistence mechanisms"
    },
    "reconnaissance": {
        "indicators": [
            "nmap",
            "netstat",
            "ss -",
            "lsof",
            "who",
            "w -",
            "last"
        ],
        "severity": "low",
        "description": "Network/system reconnaissance"
    }
}

def detect_threat_patterns(cmd):
    """
    Detect potential security threats in command
    Returns: list of detected threats
    """
    threats = []
    cmd_lower = cmd.lower()
    
    for threat_type, config in THREAT_PATTERNS.items():
        for indicator in config["indicators"]:
            if indicator in cmd_lower:
                threats.append({
                    "type": threat_type,
                    "severity": config["severity"],
                    "description": config["description"],
                    "indicator": indicator
                })
                break  # Only report each threat type once
    
    return threats


# ============= SECURITY AUDIT LOGGING =============

def _security_log():
    return get_runtime_path("SECURITY_AUDIT_PATH", "security_audit.json")


def _integrity_key() -> str:
    return os.getenv("AUDIT_INTEGRITY_KEY", os.getenv("JWT_SECRET_KEY", "")).strip()


def _canonical_audit_payload(entry: dict) -> str:
    payload = dict(entry)
    payload.pop("entry_hash", None)
    payload.pop("integrity", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest_entry(entry: dict) -> str:
    payload = _canonical_audit_payload(entry).encode("utf-8")
    key = _integrity_key()
    if key:
        return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def _entry_hash(entry: dict) -> str:
    if isinstance(entry, dict):
        if entry.get("entry_hash"):
            return str(entry["entry_hash"])
        integrity = entry.get("integrity")
        if isinstance(integrity, dict) and integrity.get("entry_hash"):
            return str(integrity["entry_hash"])
        return _digest_entry(entry)
    return ""


def _seal_entry(entry: dict, previous_entry: dict | None) -> dict:
    previous_hash = _entry_hash(previous_entry) if previous_entry else ""
    sealed = dict(entry)
    sealed["prev_hash"] = previous_hash
    sealed["integrity"] = {
        "algorithm": "hmac-sha256" if _integrity_key() else "sha256",
        "key_source": "AUDIT_INTEGRITY_KEY" if os.getenv("AUDIT_INTEGRITY_KEY", "").strip() else ("JWT_SECRET_KEY" if os.getenv("JWT_SECRET_KEY", "").strip() else "none"),
        "schema_version": 1,
    }
    sealed["entry_hash"] = _digest_entry(sealed)
    return sealed


def verify_audit_log_integrity(entries: list[dict] | None = None) -> dict:
    """
    Verify the audit hash chain. Legacy entries without entry_hash are allowed
    but counted as unsealed so the UI can report the remaining evidence gap.
    """
    if entries is None:
        try:
            with open(_security_log(), "r") as f:
                entries = json.load(f)
        except Exception as exc:
            return {
                "ok": False,
                "verified_entries": 0,
                "unsealed_entries": 0,
                "error": f"audit log unavailable: {exc}",
            }

    if not isinstance(entries, list):
        return {"ok": False, "verified_entries": 0, "unsealed_entries": 0, "error": "audit log is not a list"}

    previous_hash = ""
    verified = 0
    unsealed = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return {"ok": False, "verified_entries": verified, "unsealed_entries": unsealed, "error": f"entry {index} is not an object"}

        if not entry.get("entry_hash"):
            unsealed += 1
            previous_hash = _digest_entry(entry)
            continue

        if entry.get("prev_hash", "") != previous_hash:
            return {"ok": False, "verified_entries": verified, "unsealed_entries": unsealed, "error": f"entry {index} previous hash mismatch"}

        expected_hash = _digest_entry(entry)
        if not hmac.compare_digest(str(entry.get("entry_hash", "")), expected_hash):
            return {"ok": False, "verified_entries": verified, "unsealed_entries": unsealed, "error": f"entry {index} hash mismatch"}

        verified += 1
        previous_hash = str(entry["entry_hash"])

    return {
        "ok": True,
        "verified_entries": verified,
        "unsealed_entries": unsealed,
        "algorithm": "hmac-sha256" if _integrity_key() else "sha256",
        "keyed": bool(_integrity_key()),
    }


def security_audit_log(event_type, cmd, query, risk_analysis, threats, decision):
    """
    Log security events for audit and analysis
    """
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event_type": event_type,  # "suggested", "blocked", "approved", "executed"
        "cmd": cmd,
        "command": cmd,
        "query": query,
        "risk_level": risk_analysis["risk"],
        "risk_analysis": risk_analysis,
        "blast_radius": risk_analysis["blast_radius"],
        "threats": threats,
        "threats_detected": threats,
        "decision": decision,
        "user": "manoj"  # Could be dynamic
    }
    
    # Load existing log
    try:
        security_log = _security_log()
        if Path(security_log).exists():
            with open(security_log, "r") as f:
                logs = json.load(f)
        else:
            logs = []
    except:
        logs = []
    
    # Append and save
    previous_entry = logs[-1] if logs else None
    logs.append(_seal_entry(entry, previous_entry))
    
    security_log = _security_log()
    security_log.parent.mkdir(parents=True, exist_ok=True)
    with open(security_log, "w") as f:
        json.dump(logs, f, indent=2)
    
    return entry


# ============= SECURITY ANALYSIS =============

def analyze_command_security(cmd, query):
    """
    Complete security analysis of a command
    Returns: dict with risk assessment, threats, recommendation
    """
    # Classify risk
    risk_analysis = classify_command_risk(cmd)
    
    # Detect threats
    threats = detect_threat_patterns(cmd)
    
    # Determine recommendation
    if risk_analysis["risk"] == "critical":
        recommendation = "block"
        reason = "Critical risk - command can cause system damage"
    elif risk_analysis["risk"] == "high" and threats:
        recommendation = "block"
        reason = f"High risk with threats detected: {[t['type'] for t in threats]}"
    elif risk_analysis["risk"] == "high":
        recommendation = "require_approval"
        reason = "High risk operation requires manual approval"
    elif threats and any(t["severity"] == "high" for t in threats):
        recommendation = "require_approval"
        reason = f"High-severity threats detected: {[t['type'] for t in threats]}"
    elif risk_analysis["risk"] == "low":
        recommendation = "auto_approve"
        reason = "Low risk read-only operation"
    else:
        recommendation = "require_approval"
        reason = "Medium risk - manual review recommended"
    
    return {
        "risk_analysis": risk_analysis,
        "threats": threats,
        "recommendation": recommendation,
        "reason": reason
    }


# ============= THREAT INTELLIGENCE =============

def get_recent_threat_summary(hours=24):
    """
    Analyze recent commands for threat patterns
    Returns summary of security events
    """
    try:
        with open(_security_log(), "r") as f:
            logs = json.load(f)
    except:
        return {"error": "No security log found"}
    
    # Filter recent logs
    cutoff = datetime.now().timestamp() - (hours * 3600)
    recent = [
        log for log in logs 
        if datetime.fromisoformat(log["timestamp"]).timestamp() > cutoff
    ]
    
    # Summarize
    summary = {
        "total_commands": len(recent),
        "blocked": len([l for l in recent if l["decision"] == "blocked"]),
        "approved": len([l for l in recent if l["decision"] == "approved"]),
        "threats_detected": sum(len(l["threats_detected"]) for l in recent),
        "high_risk_commands": len([l for l in recent if l["risk_level"] in ["high", "critical"]]),
        "threat_types": {}
    }
    
    # Count threat types
    for log in recent:
        for threat in log["threats_detected"]:
            threat_type = threat["type"]
            summary["threat_types"][threat_type] = summary["threat_types"].get(threat_type, 0) + 1
    
    return summary


# ============= COMMAND SAFETY VALIDATOR =============

def validate_command_safety(cmd):
    """
    Final safety check before execution
    Returns: (is_safe: bool, reason: str)
    """
    analysis = analyze_command_security(cmd, "")
    
    if analysis["recommendation"] == "block":
        return False, analysis["reason"]
    
    # Additional safety checks
    dangerous_chars = [";", "&&", "||", "`", "$("]
    if any(char in cmd for char in dangerous_chars):
        return False, "Command chaining detected - potential security risk"
    
    return True, "Passed safety validation"
