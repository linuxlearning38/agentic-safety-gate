#!/usr/bin/env python3
"""
AVA - DevOps AI Agent v2.1
Improved sidebar with modals
"""

from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
import chromadb
from knowledge_updater.hybrid_retrieval import HybridRetriever
import ollama
import re
import subprocess
import os
import json
import time
import logging
import threading
from datetime import datetime
from control.secure_executor import execute_command_secure, execute_tool_safe, execute_approved_command
from control.tool_registry import registry as tool_registry
from control.command_graph import match_graph, execute_graph
from control.react_loop import react_loop
from control.input_router import route_query
from control.evidence_selector import (
    select_ava_self_evidence,
    select_architecture_evidence,
    select_comparison_evidence,
    select_definition_evidence,
    select_follow_up_evidence,
    select_memory_store_evidence,
    select_memory_recall_evidence,
    select_troubleshooting_evidence,
    format_ava_self_facts_block,
)
from control.answer_planner import (
    build_ava_self_plan,
    build_architecture_plan,
    build_comparison_plan,
    build_definition_plan,
    build_follow_up_plan,
    build_memory_store_plan,
    build_memory_recall_plan,
    build_troubleshooting_plan,
)
from control.response_composer import compose_response as compose_controlled_response
from control import vuln_scanner          # Day 8 — Trivy + Lynis
from control import database as db        # Phase 5B — SQLite layer
from control.self_healer import healer    # Phase 5C — self-healing engine
from control.incident_reporter import (
    report_tool_execution,
    report_graph_execution,
    report_react_execution,
    report_approved_execution,
    get_recent_reports,
    get_report_by_id,
    get_reports_stats,
)
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt, decode_token
from control.auth import init_jwt, verify_credentials, require_admin, make_token
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_limiter.errors import RateLimitExceeded

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── Day 5: JWT Authentication ─────────────────────────────────────────────────
jwt_manager = init_jwt(app)

# ── Day 6: Rate Limiting ──────────────────────────────────────────────────────
def _rate_limit_key() -> str:
    """
    Rate limit key function.
    Authenticated requests: keyed by JWT username → per-user limits.
    Unauthenticated requests: keyed by IP address → brute force protection.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            decoded = decode_token(auth[7:], allow_expired=False)
            return f"user:{decoded.get('sub', 'unknown')}"
        except Exception:
            pass
    return f"ip:{get_remote_address()}"

def _on_rate_limit_breach(limit):
    key = _rate_limit_key()
    logger.warning(
        f"[RateLimit] BREACH: key='{key}' "
        f"limit='{limit.limit}' "
        f"endpoint='{request.endpoint}' "
        f"path='{request.path}'"
    )

RATE_LIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", os.getenv("AVA_RATE_LIMIT_STORAGE_URI", "redis://redis:6379/0"))

limiter = Limiter(
    app=app,
    key_func=_rate_limit_key,
    default_limits=["30 per minute"],
    storage_uri=RATE_LIMIT_STORAGE_URI,
    headers_enabled=True,
    swallow_errors=True,
)
logger.info(f"[RateLimit] Flask-Limiter initialised — default: 30 req/min per user, storage={RATE_LIMIT_STORAGE_URI}")


@app.before_request
def maybe_start_warmup():
    start_llm_warmup()

# Configuration
CHROMA_PATH = os.getenv("CHROMA_PATH", "/home/manoj/ava-data/chromadb")
COLLECTION_NAME = "devops_policies_v2"
HISTORY_FILE = os.getenv("HISTORY_FILE", "/home/manoj/ava-data/query_history.json")
LLM_MODEL = "qwen2.5:14b"
EMBED_MODEL = "nomic-embed-text"
LLM_WARMUP_ENABLED = os.getenv("LLM_WARMUP_ENABLED", "true").lower() == "true"
LLM_WARMUP_FILE = os.getenv("LLM_WARMUP_FILE", "/tmp/ava_qwen_warmup.lock")
LLM_WARMUP_PROMPT = os.getenv("LLM_WARMUP_PROMPT", "Reply with only: ready")

# Phase 3: Memory Layer
MEMORY_PATH = os.getenv("MEMORY_PATH", "/home/manoj/ava-data/ava_memory.json")

# Phase 5B: Webhook auth secret
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
if not WEBHOOK_SECRET:
    logger.warning("[Webhook] WEBHOOK_SECRET is not set. /webhook is disabled until a secret is configured.")
_warmup_started = False
_warmup_lock = threading.Lock()


def _api_error(message: str = "Internal server error", status: int = 500, code: str = "internal_error"):
    return jsonify({"error": message, "code": code}), status

def load_memory():
    try:
        if os.path.exists(MEMORY_PATH):
            with open(MEMORY_PATH) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Memory load failed: {e}")
    return {}

def save_memory(memory):
    try:
        with open(MEMORY_PATH, "w") as f:
            json.dump(memory, f, indent=2)
    except Exception as e:
        logger.warning(f"Memory save failed: {e}")

def update_memory_issue(query, response_summary):
    global AVA_MEMORY
    memory = load_memory()
    memory.setdefault("past_issues", []).append({
        "date": datetime.now().isoformat(),
        "query": query[:100],
        "resolution": response_summary[:200]
    })
    memory["past_issues"] = memory["past_issues"][-50:]
    save_memory(memory)
    AVA_MEMORY = memory

AVA_MEMORY = load_memory()
logger.info(f"Memory loaded: user={AVA_MEMORY.get('user', 'unknown')}, "
            f"infra={list(k for k,v in AVA_MEMORY.get('infra', {}).items() if v)}")


def _run_llm_warmup():
    logger.info(f"[Warmup] Starting background warmup for {LLM_MODEL}")
    t0 = time.time()
    try:
        ollama.chat(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": LLM_WARMUP_PROMPT}],
            options={"num_ctx": 8192, "temperature": 0.0},
            keep_alive="30m",
        )
        logger.info(f"[Warmup] {LLM_MODEL} warmed in {time.time() - t0:.2f}s")
    except Exception as e:
        logger.warning(f"[Warmup] Failed: {e}")
    finally:
        try:
            if os.path.exists(LLM_WARMUP_FILE):
                os.remove(LLM_WARMUP_FILE)
        except Exception as e:
            logger.warning(f"[Warmup] Could not remove lock file: {e}")


def start_llm_warmup():
    global _warmup_started
    if not LLM_WARMUP_ENABLED:
        return

    with _warmup_lock:
        if _warmup_started:
            return
        try:
            fd = os.open(LLM_WARMUP_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            logger.info("[Warmup] Another worker is already warming the LLM")
            _warmup_started = True
            return
        except Exception as e:
            logger.warning(f"[Warmup] Could not create lock file: {e}")
            return

        _warmup_started = True
        threading.Thread(target=_run_llm_warmup, daemon=True).start()

# Initialize ChromaDB
chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = chroma_client.get_collection(COLLECTION_NAME)

# Phase 2: Hybrid retriever (policies + blogs)
import yaml
with open("knowledge_updater/config.yaml") as _f:
    _ku_config = yaml.safe_load(_f)
hybrid_retriever = HybridRetriever(_ku_config, existing_client=chroma_client)

# Phase 5B: SQLite init (auto-migrates ava_memory.json on first run)
db.init_db()

# Phase 5C: background health monitor
from control.monitor import start_monitor
if os.getenv("AVA_MONITOR_ENABLED", "false").lower() == "true":
    start_monitor()
else:
    logger.info("[Monitor] Background health monitor disabled by default; set AVA_MONITOR_ENABLED=true to enable.")

# Command whitelist - expanded for server management
ALLOWED_COMMANDS = [
    # Basic commands
    'date', 'whoami', 'pwd', 'ls', 'cat', 'grep', 'df', 'free',
    'ps', 'top', 'uptime', 'uname', 'echo', 'head', 'tail',
    'wc', 'find', 'which', 'hostname',
    # Server management
    'ollama',      # For ollama list, ollama ps, etc.
    'docker',      # For docker ps, docker images, etc.
    'systemctl',   # For service management (status, start, stop)
    'curl',        # For API testing
    'wget',        # For downloads
    'netstat',     # For network stats
    'ss',          # Socket statistics
    'git'          # For git status, git log, etc.
]

BLOCKED_PATHS = [
    '/etc/passwd', '/etc/shadow', '/root', '~/.ssh',
    '/var/log', '/proc', '/sys'
]

# Stats
_blogs_count = 0
try:
    _blogs_col = chroma_client.get_collection("devops_blogs_v1")
    _blogs_count = _blogs_col.count()
except:
    pass

STATS = {
    'total_chunks': collection.count() + _blogs_count,
    'repos': 5,
    'model': 'Qwen 2.5 14B',
    'opa_enabled': True,
    'whitelisted_commands': len(ALLOWED_COMMANDS),
    'total_tokens': 0,
    'query_count': 0,
    'avg_tokens_per_query': 0
}

# History functions
def load_history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        return []
    except Exception as e:
        logger.error(f"Error loading history: {e}")
        return []

def save_history(entry):
    try:
        history = load_history()
        history.append(entry)
        history = history[-100:]
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving history: {e}")

# Safety functions
def is_command_safe(cmd):
    cmd_parts = cmd.strip().split()
    if not cmd_parts:
        return False, "Empty command"
    
    base_cmd = cmd_parts[0]
    if base_cmd not in ALLOWED_COMMANDS:
        return False, f"Command '{base_cmd}' not in whitelist"
    
    for path in BLOCKED_PATHS:
        if path in cmd:
            return False, f"Access to '{path}' is blocked"
    
    dangerous_patterns = ['rm', 'sudo', '>', '>>', '&', ';']
    for pattern in dangerous_patterns:
        if pattern in cmd:
            return False, f"Dangerous pattern '{pattern}' detected"
    
    # Check for pipe - allow only when used with grep (safe filtering)
    if '|' in cmd and 'grep' not in cmd:
        return False, "Pipe (|) is only allowed with grep for filtering"
    
    return True, "Command is safe"

def execute_command(cmd, query=""):
    """Execute command with AgentGuard security controls"""
    result = execute_tool_safe("raw_command", {"command": cmd}, query=query, source="ask_command")

    if result["status"] == "success":
        return {
            'success': True,
            'blocked': False,
            'output': result.get("output", ""),
            'error': result.get("error", ""),
            'returncode': 0,
            'security': {
                'risk': result.get("risk"),
                'threats': result.get("threats", [])
            },
            'command_repr': result.get("command_repr", cmd),
        }
    
    elif result["status"] == "blocked":
        return {
            'success': False,
            'blocked': True,
            'reason': result["reason"],
            'security': {
                'risk': result["risk"],
                'threats': result.get("threats", [])
            },
            'suggestion': 'Command blocked by AgentGuard security policy.'
        }
    
    elif result["status"] == "approval_required":
        return {
            'success': False,
            'blocked': False,
            'approval_required': True,
            'approval_id': result["approval_id"],
            'reason': result.get("reason", ""),
            'command': cmd,
            'security': {
                'risk': result["risk"],
                'blast_radius': result.get("blast_radius"),
                'threats': result.get("threats", [])
            },
            'suggestion': f'⚠️  Run: python3 control/security_review.py (ID: {result["approval_id"]})'
        }
    
    else:
        return {
            'success': False,
            'blocked': False,
            'reason': result.get("reason") or result.get("error") or "Unknown error",
            'error': result.get("error", ""),
            'command': result.get("command_repr", cmd),
            'security': {
                'risk': result.get("risk"),
                'threats': result.get("threats", []),
                'blast_radius': result.get("blast_radius"),
            },
        }


def _build_command_response(exec_result: dict):
    return {
        'success': exec_result.get('status') == 'success',
        'blocked': exec_result.get('status') == 'blocked',
        'approval_required': exec_result.get('status') == 'approval_required',
        'approval_id': exec_result.get('approval_id'),
        'output': exec_result.get('output', ''),
        'error': exec_result.get('error', ''),
        'reason': exec_result.get('reason', ''),
        'command': exec_result.get('command_repr', ''),
        'security': {
            'risk': exec_result.get('risk'),
            'threats': exec_result.get('threats', []),
            'blast_radius': exec_result.get('blast_radius'),
        },
        'metadata': exec_result.get('metadata', {}),
    }


def _command_response_text(result: dict) -> str:
    status = result.get("status")
    risk = (result.get("risk") or "unknown").upper()
    command = result.get("command_repr") or result.get("command") or "requested action"
    if status == "approval_required":
        approval_id = result.get("approval_id") or "pending"
        return (
            f"Approval required for {risk.lower()}-risk action.\n"
            f"Action: {command}\n"
            f"Approval ID: {approval_id}"
        )
    if status == "blocked":
        return result.get("reason") or result.get("error") or "Action blocked by security policy."
    return result.get("output") or result.get("error") or result.get("reason") or ""


def _is_compound_dangerous_request(query: str) -> bool:
    q = f" {_normalize_user_query(query).lower()} "
    dangerous_markers = (
        " rm -rf ", " delete ", " drain ", " drop ", " truncate ",
        " shutdown ", " wipe ", " destroy ", " format ",
    )
    hits = sum(1 for marker in dangerous_markers if marker in q)
    return hits >= 2


_LEARNING_PREFIXES = (
    "how ", "how do ", "how to ", "how can ", "how would ", "how does ",
    "what ", "what is ", "what are ", "what does ", "what would ", "what happens ",
    "why ", "why does ", "why would ", "why is ",
    "explain ", "tell me about ", "describe ", "can you explain",
    "could you explain", "show me how", "walk me through",
)


def _is_learning_query(q: str) -> bool:
    """Return True when the query is asking to learn, not requesting execution."""
    return any(q.startswith(prefix) for prefix in _LEARNING_PREFIXES)


def _is_single_destructive_request(query: str) -> bool:
    q_raw   = (query or "").strip()
    q_plain = _normalize_user_query(q_raw).lower().strip()
    q       = f" {q_plain} "   # padded for word-boundary substring matching

    # Learning queries are informational — user wants to understand, not execute
    if _is_learning_query(q_plain):
        return False

    # ── 1. Legacy patterns (preserved exactly) ────────────────────────────────
    legacy_patterns = (
        (" delete ", (" service ", " deployment ", " pod ", " namespace ", " database ", " cluster ")),
        (" drop ",   (" table ", " tables ", " database ", " schema ")),
        (" truncate ", (" table ", " tables ", " database ")),
        (" wipe ",   (" disk ", " database ", " service ", " deployment ")),
        (" destroy ", (" service ", " deployment ", " database ", " cluster ")),
        (" format ", (" disk ", " drive ", " volume ")),
    )
    for action, targets in legacy_patterns:
        if action in q and any(target in q for target in targets):
            return True

    # ── 2. Mass deletion ──────────────────────────────────────────────────────
    if "delete all " in q_plain:
        return True
    if " delete " in q and " --all" in q:
        return True
    if "kubectl delete --all" in q_plain:
        return True
    if " kill all " in q and any(r in q for r in (" containers ", " pods ", " processes ")):
        return True

    # ── 3. rm -rf on critical system paths ────────────────────────────────────
    for variant in (
        "rm -rf /", "rm -rf /*", "rm -rf /home", "rm -rf /var",
        "rm -rf /etc", "rm -rf /usr", "rm -rf /boot", "rm -rf /root",
        "rm -r /", "rm --force /",
    ):
        if variant in q_plain:
            return True

    # ── 4. Disk destruction ───────────────────────────────────────────────────
    if "format /dev/" in q_plain:
        return True
    if "mkfs" in q_plain:
        return True
    if "wipefs" in q_plain:
        return True
    if "shred /dev/" in q_plain:
        return True
    if "fdisk /dev/" in q_plain:
        return True
    if "dd if=" in q_plain and "of=/dev/" in q_plain:
        return True

    # ── 5. Critical system file overwrite ─────────────────────────────────────
    for critical_file in (
        "/etc/passwd", "/etc/shadow", "/etc/sudoers", "/etc/fstab", "/etc/hosts",
    ):
        if ("> "  + critical_file) in q_plain:
            return True
        if (">>" + critical_file) in q_plain:
            return True
        if re.search(r"echo\b.*>\s*" + re.escape(critical_file), q_plain):
            return True
    if "> /boot/" in q_plain:
        return True

    # ── 6. Permissions / auth destruction ─────────────────────────────────────
    system_paths = ("/", "/etc", "/usr", "/bin", "/sbin", "/boot", "/root")
    if re.search(r"\bchmod\b.*(777|000|0777|a\+rwx|ugo\+rwx)", q_plain):
        if any(p in q_plain for p in system_paths):
            return True
    if re.search(r"\bchown\b.+-[rR].+root.*/", q_plain, re.IGNORECASE):
        return True
    if re.search(r"\bchown\b.+-[rR].*\s+\d+:\d+\s+/", q_plain):
        return True
    if "usermod -l root" in q_plain or "passwd -d root" in q_plain:
        return True

    # ── 7. System control ─────────────────────────────────────────────────────
    for cmd in ("shutdown", "halt", "poweroff", "reboot -f", "init 0", "init 6", "kill -9 -1", "killall5"):
        if (
            q_plain == cmd
            or q_plain.startswith(f"{cmd} ")
            or q_plain.endswith(f" {cmd}")
            or f" {cmd} " in q
        ):
            return True

    # ── 8. Fork bomb ──────────────────────────────────────────────────────────
    # Check raw query — normalization strips special chars
    if ":(){ :|:& };" in q_raw or ":(){ :|: & };" in q_raw:
        return True

    return False


def _blocked_action_result(query: str, reason: str, threat: str, blast_radius: str = "critical") -> dict:
    return {
        "status": "blocked",
        "risk": "critical",
        "reason": reason,
        "command_repr": _normalize_user_query(query),
        "approval_id": None,
        "threats": [threat],
        "blast_radius": blast_radius,
    }


def _resolve_direct_action_query(query: str) -> dict | None:
    # Destructive check MUST come before extract_explicit_command_request.
    # 'echo' and 'kill' are both in _RAW_COMMAND_STARTERS — without this order,
    # they get routed to execute_command_secure which returns approval_required
    # instead of critical-blocked.
    if _is_single_destructive_request(query):
        blocked_result = _blocked_action_result(
            query,
            "Destructive infrastructure or database requests are blocked by policy.",
            "destructive_request",
        )
        return {
            "kind": "command",
            "result": blocked_result,
            "response": _command_response_text(blocked_result),
        }

    # Vague diagnostics like "find problems" must clarify before raw command
    # extraction, otherwise "find" is treated as a shell command starter.
    if _is_vague_diagnostic_query(query):
        return {
            "kind": "knowledge",
            "response": _build_vague_diagnostic_clarification(),
            "confidence": "high",
        }

    explicit_command = extract_explicit_command_request(query)
    if explicit_command:
        result = execute_tool_safe(
            "raw_command",
            {"command": explicit_command},
            query=query,
            source="ask_command",
        )
        return {
            "kind": "command",
            "result": result,
            "response": _command_response_text(result),
        }

    operational_tool = extract_operational_tool_request(query)
    if operational_tool:
        exec_result = execute_tool_safe(
            operational_tool["tool_name"],
            operational_tool["tool_args"],
            query=query,
            source="ask_operational",
        )
        return {
            "kind": "command",
            "result": exec_result,
            "response": _command_response_text(exec_result),
        }

    operational_clarification = extract_operational_clarification(query)
    if operational_clarification:
        return {
            "kind": "knowledge",
            "response": operational_clarification,
            "confidence": "high",
        }

    llm_operational = _classify_operational_intent_with_llm(query)
    if llm_operational:
        if llm_operational["decision"] == "clarification":
            return {
                "kind": "knowledge",
                "response": llm_operational["clarification"],
                "confidence": llm_operational["confidence"],
            }

        exec_result = execute_tool_safe(
            llm_operational["tool_name"],
            llm_operational["tool_args"],
            query=query,
            source="ask_operational_llm_fallback",
        )
        return {
            "kind": "command",
            "result": exec_result,
            "response": _command_response_text(exec_result),
        }

    return None


_RAW_COMMAND_PREFIXES = (
    "run ",
    "execute ",
    "shell ",
    "cmd ",
    "command ",
)

_RAW_COMMAND_STARTERS = (
    "df", "free", "ps", "top", "uptime", "uname", "whoami", "pwd", "ls", "cat",
    "grep", "find", "head", "tail", "wc", "which", "hostname", "echo", "git",
    "docker", "kubectl", "helm", "terraform", "systemctl", "service", "journalctl",
    "curl", "wget", "ssh", "scp", "bash", "sh", "python", "python3", "rm", "mv",
    "cp", "chmod", "chown", "kill", "pkill", "netstat", "ss",
)


def extract_explicit_command_request(query: str) -> str | None:
    q = _normalize_user_query(query).strip()
    if not q:
        return None

    lower = q.lower()
    for prefix in _RAW_COMMAND_PREFIXES:
        if lower.startswith(prefix):
            candidate = q[len(prefix):].strip()
            return candidate or None

    first_token = q.split()[0].lower()
    if first_token in _RAW_COMMAND_STARTERS:
        return q

    return None


def looks_like_operational_request(query: str) -> bool:
    q = _normalize_user_query(query).strip().lower()
    if not q:
        return False

    action_terms = (
        "show", "check", "list", "get", "restart", "scale", "describe",
        "scan", "inspect", "tail", "view", "display", "run", "execute",
    )
    target_terms = (
        "disk", "memory", "pod", "pods", "node", "nodes", "service", "services",
        "deployment", "deployments", "container", "containers", "docker",
        "kubernetes", "cluster", "logs", "log", "status", "health", "image",
        "process", "processes", "port", "ports", "update", "updates", "package",
        "packages", "security", "auth", "ssh", "vulnerability", "vulnerabilities",
        "cve", "cves", "suspicious",
    )

    first_word = q.split()[0]
    if first_word not in action_terms:
        return False

    return any(term in q for term in target_terms)


def _is_vague_diagnostic_query(query: str) -> bool:
    q = _normalize_user_query(query).strip().lower()
    if not q:
        return False

    if extract_operational_tool_request(q):
        return False
    if extract_operational_clarification(q):
        return False
    if q.startswith(_LEARNING_PREFIXES):
        return False

    exact_matches = {
        "find problems",
        "find issues",
        "find bugs",
        "find something",
        "check stuff",
        "check things",
        "something is wrong",
        "something broke",
        "look for issues",
        "diagnose",
        "troubleshoot",
    }
    if q in exact_matches:
        return True

    return q in {"what is wrong", "what's wrong"} or q.startswith("what is wrong ") or q.startswith("what's wrong ")


def _build_vague_diagnostic_clarification() -> str:
    return (
        "I can check for problems in several ways. Try one of these:\n\n"
        "'is anything suspicious on this system' (security check)\n"
        "'verify my system' (health check: disk, memory, docker, containers)\n"
        "'check failed services' (failed services check)\n"
        "'scan my system for vulnerabilities' (CVE check)\n"
        "'show running processes' (process list)\n\n"
        "What kind of check would you like?"
    )


def _extract_json_object(text: str) -> dict | None:
    raw = _normalize_text(text).strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _should_try_operational_intent_classifier(query: str) -> bool:
    q = _normalize_user_query(query).strip()
    if not q:
        return False

    route = _route_query(q)
    if route.intent in {
        "ava_self",
        "memory_store",
        "memory_recall",
        "architecture",
        "follow_up",
        "comparison",
        "definition",
    }:
        return False

    lower = q.lower()
    if _is_learning_query(lower):
        return False
    if extract_explicit_command_request(q):
        return False
    if extract_operational_tool_request(q):
        return False
    if extract_operational_clarification(q):
        return False
    if _is_vague_diagnostic_query(q):
        return False

    ops_markers = (
        "service", "services", "docker", "container", "containers", "host", "system",
        "machine", "listener", "listeners", "ports", "process", "processes", "patch",
        "patching", "updates", "update", "security", "auth", "ssh", "failed",
        "failure", "failures", "suspicious", "vulnerab", "cve", "health", "healthy",
        "inspect", "investigate", "check", "verify", "scan", "look for",
    )
    return any(marker in lower for marker in ops_markers)


def _classify_operational_intent_with_llm(query: str) -> dict | None:
    if not _should_try_operational_intent_classifier(query):
        return None

    system_prompt = (
        "You are AVA's operational intent classifier. "
        "Decide whether a natural-language infrastructure request should map to one existing operational tool, "
        "ask for clarification, or return none. Never invent new tools. Return JSON only."
    )
    user_prompt = f"""
Classify this query:
{query}

Allowed tools:
- verify_system
- assess_host_risk
- check_suspicious_activity
- check_failed_services
- check_updates
- scan_host_vulnerabilities
- show_processes
- show_listening_ports
- check_auth_events
- inspect_service

Rules:
- Return one JSON object only.
- Schema:
  {{"decision":"tool|clarification|none","tool_name":"","tool_args":{{}},"clarification":"","confidence":"low|medium|high"}}
- Use decision="tool" only if the query clearly maps to exactly one allowed tool.
- Use inspect_service only when a service name is present.
- Use decision="clarification" if the user wants a service inspection but did not provide the service name.
- Use decision="none" if this is not clearly an operational tool request.

Examples:
- "do I have any failed services" -> check_failed_services
- "what should I investigate on this host" -> assess_host_risk
- "look for suspicious activity" -> check_suspicious_activity
- "check if my machine needs patching" -> check_updates
- "can you inspect nginx service health" -> inspect_service with service="nginx"
- "inspect my service" -> clarification
""".strip()

    try:
        response = ollama.chat(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"num_ctx": 2048, "temperature": 0.0},
        )
    except Exception as e:
        logger.warning(f"[IntentClassifier] LLM fallback failed: {e}")
        return None

    payload = _extract_json_object(response.get("message", {}).get("content", ""))
    if not payload:
        return None

    decision = (payload.get("decision") or "none").strip().lower()
    confidence = (payload.get("confidence") or "low").strip().lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"

    if decision == "clarification":
        clarification = _normalize_text(payload.get("clarification")).strip()
        if clarification:
            return {
                "decision": "clarification",
                "clarification": clarification,
                "confidence": confidence,
            }
        return None

    if decision != "tool":
        return None

    tool_name = _normalize_text(payload.get("tool_name")).strip()
    tool_args = payload.get("tool_args") if isinstance(payload.get("tool_args"), dict) else {}
    allowed_tools = {
        "verify_system",
        "assess_host_risk",
        "check_suspicious_activity",
        "check_failed_services",
        "check_updates",
        "scan_host_vulnerabilities",
        "show_processes",
        "show_listening_ports",
        "check_auth_events",
        "inspect_service",
    }
    if tool_name not in allowed_tools:
        return None

    if tool_name == "inspect_service":
        service_name = _normalize_text(tool_args.get("service")).strip()
        if not service_name:
            return None
        tool_args = {"service": service_name}

    return {
        "decision": "tool",
        "tool_name": tool_name,
        "tool_args": tool_args,
        "confidence": confidence,
    }


def extract_operational_tool_request(query: str) -> dict | None:
    q = _normalize_user_query(query).strip()
    if not q:
        return None

    lower = q.lower()

    namespace_match = re.search(r"\b(?:in|from|for)\s+namespace\s+([a-z0-9-]+)\b", lower)
    namespace = namespace_match.group(1) if namespace_match else "default"

    scale_match = re.search(
        r"\bscale\s+(?:the\s+)?deployment\s+(?!to\b)([a-z0-9][a-z0-9._-]*)\s+(?:to\s+)?(\d+)\s+replicas?\b",
        lower,
    )
    if scale_match:
        return {
            "tool_name": "scale_deployment",
            "tool_args": {
                "deployment": scale_match.group(1),
                "replicas": int(scale_match.group(2)),
                "namespace": namespace,
            },
        }

    rollback_match = re.search(
        r"\brollback\s+(?:the\s+)?deployment\s+(?!my\b|the\b)([a-z0-9][a-z0-9._-]*)\b|\brollback\s+(?!my\b|the\b)([a-z0-9][a-z0-9._-]*)\s+deployment\b",
        lower,
    )
    if rollback_match:
        deployment_name = rollback_match.group(1) or rollback_match.group(2)
        return {
            "tool_name": "rollback_deployment",
            "tool_args": {
                "deployment": deployment_name,
                "namespace": namespace,
            },
        }

    restart_pod_match = re.search(
        r"\brestart\s+(?:the\s+)?(?:pod|deployment)\s+(?!my\b|the\b)([a-z0-9][a-z0-9._-]*)\b",
        lower,
    )
    restart_named_pod_match = re.search(
        r"\brestart\s+(?!my\b|the\b)([a-z0-9][a-z0-9._-]*)\s+(?:pod|deployment)\b",
        lower,
    )
    restart_target = None
    if restart_pod_match:
        restart_target = restart_pod_match.group(1)
    elif restart_named_pod_match:
        restart_target = restart_named_pod_match.group(1)

    if restart_target:
        return {
            "tool_name": "restart_pod",
            "tool_args": {
                "deployment": restart_target,
                "namespace": namespace,
            },
        }

    restart_service_match = re.search(
        r"\brestart\s+(?:the\s+)?(?:service\s+)?([a-z0-9][a-z0-9._-]*)\s+service\b|\brestart\s+service\s+([a-z0-9][a-z0-9._-]*)\b",
        lower,
    )
    if restart_service_match:
        service_name = restart_service_match.group(1) or restart_service_match.group(2)
        if service_name:
            return {
                "tool_name": "restart_service",
                "tool_args": {"service": service_name},
            }

    docker_service_restart_phrases = (
        "restart docker service",
        "restart the docker service",
        "restart my docker service",
        "restart docker daemon",
    )
    if any(phrase in lower for phrase in docker_service_restart_phrases):
        return {
            "tool_name": "restart_service",
            "tool_args": {"service": "docker"},
        }

    if any(phrase in lower for phrase in ("show disk usage", "check disk usage", "list disk usage", "disk usage", "check disk")):
        return {"tool_name": "check_disk", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "show memory usage", "check memory usage", "memory usage", "check memory",
        "check my memory", "show my memory", "memory status", "ram usage", "check ram",
    )):
        return {"tool_name": "check_memory", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "check host telemetry", "show host telemetry", "host telemetry",
        "show real host facts", "check real host facts", "read host facts",
        "show host facts",
    )):
        return {"tool_name": "check_host_telemetry", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "verify my system", "check my system", "verify system", "system check",
        "check system health", "system health", "what's wrong with my system",
        "what is wrong with my system", "diagnose my system", "inspect my system",
    )):
        return {"tool_name": "verify_system", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "check docker", "show docker status", "docker status", "verify docker",
        "is my docker running", "is docker running", "is my docker running correctly",
        "docker daemon is not responding", "docker not responding", "check my docker",
        "is docker healthy",
    )):
        return {"tool_name": "check_docker", "tool_args": {}}

    if any(phrase in lower for phrase in ("show running containers", "list running containers", "show containers", "list containers")):
        return {"tool_name": "list_containers", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "show running processes", "show processes", "check processes", "list processes",
        "top processes", "show top processes", "show unusual processes",
    )):
        return {"tool_name": "check_processes", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "show listening ports", "check listening ports", "show open ports", "check open ports",
        "list open ports", "list listening ports", "which ports are open",
    )):
        return {"tool_name": "check_listening_ports", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "show failed services", "check failed services", "failed services", "which services failed",
    )):
        return {"tool_name": "check_failed_services", "tool_args": {}}

    inspect_service_match = re.search(
        r"\b(?:inspect|investigate|check)\s+(?:the\s+)?service\s+([a-z0-9][a-z0-9._-]*)\b",
        lower,
    )
    if inspect_service_match:
        return {"tool_name": "inspect_service", "tool_args": {"service": inspect_service_match.group(1)}}

    if any(phrase in lower for phrase in (
        "check auth failures", "show auth failures", "check login failures", "show login failures",
        "check ssh failures", "show ssh failures", "failed logins",
    )):
        return {"tool_name": "check_auth_events", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "check persistence points", "show persistence points", "check cron jobs and timers",
        "inspect persistence", "review cron and timers",
    )):
        return {"tool_name": "check_persistence_points", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "check for updates", "show updates", "show package updates", "check package updates",
        "show security updates", "check security updates", "what packages need updates",
    )):
        return {"tool_name": "check_updates", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "assess host risk", "assess my host risk", "overall host risk",
        "what should i investigate on this host", "what is the biggest risk on this system",
        "summarize host risk", "show host risk",
    )):
        return {"tool_name": "assess_host_risk", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "install security updates", "apply security updates", "patch my system",
        "patch the system", "install package updates", "apply package updates",
        "update my system",
    )):
        return {"tool_name": "install_updates", "tool_args": {}}

    patch_package_match = re.search(
        r"\b(?:patch|update|upgrade)\s+package\s+([a-z0-9][a-z0-9+._:-]*)\b",
        lower,
    )
    if patch_package_match:
        return {"tool_name": "patch_package", "tool_args": {"package": patch_package_match.group(1)}}

    if any(phrase in lower for phrase in (
        "scan my system for vulnerabilities", "scan the system for vulnerabilities",
        "show cves affecting this host", "show cves on this host", "check vulnerabilities",
        "check cves", "scan host vulnerabilities", "vulnerability scan",
    )):
        return {"tool_name": "scan_host_vulnerabilities", "tool_args": {}}

    if any(phrase in lower for phrase in (
        "is anything suspicious", "is anything suspicious on this system", "check suspicious activity",
        "review recent security events", "inspect suspicious activity", "check for suspicious activity",
        "is my system suspicious",
    )):
        return {"tool_name": "check_suspicious_activity", "tool_args": {}}

    stop_process_match = re.search(
        r"\b(?:stop|kill|terminate)\s+(?:the\s+)?(?:suspicious\s+)?process\s+(\d+)\b",
        lower,
    )
    if stop_process_match:
        return {
            "tool_name": "stop_process",
            "tool_args": {"pid": int(stop_process_match.group(1))},
        }

    inspect_process_match = re.search(
        r"\b(?:inspect|investigate|check)\s+(?:the\s+)?process\s+(\d+)\b",
        lower,
    )
    if inspect_process_match:
        return {
            "tool_name": "inspect_process",
            "tool_args": {"pid": int(inspect_process_match.group(1))},
        }

    if any(phrase in lower for phrase in ("show pod status", "check pod status", "list pods", "get pods", "show pods")):
        return {
            "tool_name": "check_pod_status",
            "tool_args": {"namespace": namespace},
        }

    logs_match = re.search(
        r"\b(?:show|check|get|tail|view)\s+(?:the\s+)?logs?\s+(?:for\s+)?(?:pod\s+)?([a-z0-9][a-z0-9._-]*)\b",
        lower,
    )
    if logs_match:
        return {
            "tool_name": "check_logs",
            "tool_args": {"pod_name": logs_match.group(1), "namespace": namespace},
        }

    describe_match = re.search(
        r"\b(?:describe|inspect|check)\s+(?:the\s+)?pod\s+([a-z0-9][a-z0-9._-]*)\b",
        lower,
    )
    if describe_match:
        return {
            "tool_name": "check_pod_describe",
            "tool_args": {"pod_name": describe_match.group(1), "namespace": namespace},
        }

    service_status_match = re.search(
        r"\b(?:show|check|get)\s+(?:the\s+)?status\s+(?:of\s+)?(?:service\s+)?([a-z0-9][a-z0-9._-]*)\b",
        lower,
    )
    if service_status_match and "pod" not in lower:
        return {
            "tool_name": "check_service_health",
            "tool_args": {"service": service_status_match.group(1)},
        }

    node_status_phrases = ("show node status", "check node status", "list nodes", "get nodes", "show cluster nodes")
    if any(phrase in lower for phrase in node_status_phrases):
        return {"tool_name": "check_node_status", "tool_args": {}}

    return None


def extract_operational_clarification(query: str) -> str | None:
    q = _normalize_text(query).strip().lower()
    if not q:
        return None

    if re.search(r"\bscale\s+(?:the\s+)?deployment\s+to\s+\d+\s+replicas?\b", q):
        return "I can queue a deployment scale action, but I need the deployment name. Example: scale deployment nginx to 5 replicas."

    if re.search(r"\brollback\s+(?:my\s+|the\s+)?deployment\b", q):
        return "I can queue a deployment rollback, but I need the deployment name. Example: rollback deployment nginx."

    if "restart my service" in q or "restart the service" in q:
        return "I can queue a service restart, but I need the service name. Example: restart service docker."

    if q in {"restart my pod", "restart the pod", "restart deployment"}:
        return "I can queue a deployment restart, but I need the deployment name. Example: restart the pod nginx."

    if q in {"show me pod logs", "show pod logs", "get pod logs", "check pod logs"}:
        return "I can fetch pod logs, but I need the pod name. Example: show me pod logs for nginx-7d8b49557c-abc12."

    if q in {"check my service", "check service", "show my service", "show service"}:
        return "I can check a service, but I need the service name. Example: check service api-gateway."

    if q in {"restart my deployment", "restart the deployment"}:
        return "I can queue a deployment restart, but I need the deployment name. Example: restart deployment nginx."

    if q in {"stop suspicious process", "stop process", "kill process", "terminate process"}:
        return "I can queue a process stop action, but I need the PID. Example: stop suspicious process 4321."

    if q in {"patch package", "update package", "upgrade package"}:
        return "I can queue a package patch action, but I need the package name. Example: patch package openssl."

    if "show me the result" in q and "restart" in q and not re.search(r"\brestart\b.+\b(?:docker|service|pod|deployment)\s+[a-z0-9._-]+\b", q):
        return "I can queue the restart, but I need the exact target first. Example: restart service docker."

    return None

def get_embedding(text):
    try:
        response = ollama.embeddings(model=EMBED_MODEL, prompt=text)
        return response['embedding']
    except Exception as e:
        logger.error(f"Embedding error: {e}")
        return None

def query_knowledge_base(query, n_results=5, n_policies=4, n_blogs=6, min_relevance=0.003, query_intent=None):
    """Phase 3: Hybrid retrieval with context assembly."""
    try:
        if query_intent is None:
            query_intent = detect_query_intent(query)
        entities = _extract_query_entities(query)

        # ava_self: controlled migration path — authoritative runtime facts only
        if query_intent == "ava_self":
            about = _get_about_data()
            return [format_ava_self_facts_block(about)]

        if query_intent == "definition":
            n_policies = max(n_policies, 6)
            n_blogs = min(n_blogs, 2)
            min_relevance = max(min_relevance, 0.01)

        raw_chunks = hybrid_retriever.query(
            query_text=query,
            n_policies=n_policies,
            n_blogs=n_blogs,
            blog_min_relevance=min_relevance,
            format_for_llm=False
        )
        if entities:
            boosted_chunks = []
            keyword_limit = 4
            for collection in [
                getattr(hybrid_retriever, "patterns_collection", None),
                getattr(hybrid_retriever, "fixes_collection", None),
                getattr(hybrid_retriever, "policies_collection", None),
                getattr(hybrid_retriever, "blogs_collection", None),
            ]:
                boosted_chunks.extend(hybrid_retriever._keyword_fetch(collection, entities, limit=keyword_limit))
            if boosted_chunks:
                seen = {chunk.content for chunk in (raw_chunks or [])}
                for chunk in boosted_chunks:
                    if chunk.content not in seen:
                        raw_chunks.append(chunk)
                        seen.add(chunk.content)
        if raw_chunks:
            assembled = hybrid_retriever.assemble_context(raw_chunks)
            if assembled:
                if _needs_strict_grounding(query_intent, query):
                    assembled = [_build_grounding_block(query, assembled, query_intent)] + assembled
                logger.info(f"Context assembly: {len(raw_chunks)} chunks -> {len(assembled)} merged blocks")
                return assembled
            raw_docs = [hybrid_retriever._strip_section_labels(chunk.content) for chunk in raw_chunks]
            if _needs_strict_grounding(query_intent, query):
                raw_docs = [_build_grounding_block(query, raw_docs, query_intent)] + raw_docs
            return raw_docs
        embedding = get_embedding(query)
        if not embedding:
            return []
        results = hybrid_retriever.policies_collection.query(query_embeddings=[embedding], n_results=n_results)
        return results["documents"][0] if results["documents"] else []
    except Exception as e:
        logger.error(f"Query error: {e}")
        return []

def is_technical_query(query):
    signals = ["fix", "error", "failed", "issue", "not working", "debug", "broken",
               "problem", "crash", "warning", "exception", "configure", "setup", "how to"]
    return any(s in query.lower() for s in signals)

def is_complex_query(query):
    cot_keywords = ["why", "how", "explain", "design", "compare", "difference",
                    "should i", "best way", "which", "when"]
    return len(query.split()) > 10 or any(k in query.lower() for k in cot_keywords)

def is_weak_response(response_text):
    weak_signals = ["i don't have", "i don't know", "not found", "no information",
                    "unable to find", "cannot find", "not in my knowledge",
                    "i cannot", "no relevant", "i'm not sure", "i am not sure",
                    "i do not have access"]
    return any(signal in response_text.lower() for signal in weak_signals)

_MEMORY_FACT_KEY = "chat_facts"
_ENTITY_STOP_WORDS = {
    "what", "which", "when", "where", "why", "how", "the", "this", "that",
    "with", "from", "into", "about", "only", "using", "between", "difference",
    "previous", "asked", "mentioned", "remember", "exactly", "respond", "format",
    "json", "risk", "level", "rollback", "action", "taken", "command", "confidence",
    "issue", "type", "your", "ava", "please", "show", "tell", "explain", "describe",
}
_INFRA_COMPONENTS = [
    "aws", "azure", "gcp", "kubernetes", "docker", "linux", "terraform",
    "eks", "aks", "gke", "ec2", "ecs", "ecr", "lambda", "s3", "rds",
    "vpc", "iam", "route53", "cloudfront", "alb", "nlb", "virtual network",
    "subnet", "application gateway", "cosmos db", "service bus", "event hub",
    "blob storage", "cloud sql", "bigquery", "pubsub", "cloud run",
    "cloud functions", "gcs", "firestore", "ingress", "service mesh", "istio",
    "envoy", "helm", "prometheus", "grafana", "argo", "jenkins", "nginx",
    "redis", "postgres", "mysql", "cassandra", "elasticsearch", "spark",
    "kafka", "samza", "mantis", "zuul", "resilience4j", "evcache", "netty",
    "cdn", "open connect",
]
def _normalize_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)

def _normalize_user_query(query):
    text = _normalize_text(query).strip()
    if not text:
        return ""
    # Strip repeated list, outline, and punctuation-heavy prefixes such as:
    # "1. ", "2) ", "2.1.2. ", "a.b.c. ", "--- ", or mixed copied numbering junk.
    patterns = [
        r'^(?:(?:\s*[\[\(]?\d+(?:\.\d+)*[\]\)]?[\.\):-]*\s*)+)',
        r'^(?:(?:\s*[a-zA-Z](?:\.[a-zA-Z0-9]+)*[\.\):-]+\s*)+)',
        r'^(?:\s*[-_=:#*~`|\\/]+\s*)+',
        r'^(?:\s*(?:\d+[a-zA-Z]?|[a-zA-Z]{1,3})[-_=]+\s*)+',
    ]
    previous = None
    while text and text != previous:
        previous = text
        for pattern in patterns:
            text = re.sub(pattern, '', text).strip()
    # If the query still contains a question trigger later in the string, trim to it.
    question_markers = [
        "what ", "which ", "how ", "why ", "when ", "where ",
        "explain ", "compare ", "define ", "remember ", "draw ",
        "create ", "show ", "tell ",
    ]
    lower = text.lower()
    starts_with_marker = any(lower.startswith(marker) for marker in question_markers)
    positions = [lower.find(marker) for marker in question_markers if lower.find(marker) > 0]
    if positions and not starts_with_marker:
        candidate = text[min(positions):].strip()
        if len(candidate.split()) >= 2:
            text = candidate
    return text.strip()


def _route_query(query):
    return route_query(
        query,
        normalizer=_normalize_user_query,
        entity_extractor=_extract_query_entities,
        memory_request_extractor=_extract_memory_request,
        recall_label_extractor=_extract_recall_label,
    )


def _resolve_ava_self_response(query, about=None):
    route = _route_query(query)
    if route.intent != "ava_self":
        return None
    evidence = select_ava_self_evidence(route, about or _get_about_data())
    plan = build_ava_self_plan(route, evidence)
    return compose_controlled_response(plan)


def _resolve_memory_store_response(query):
    route = _route_query(query)
    if route.intent != "memory_store" or not route.memory_fact:
        return None
    saved_fact = _save_chat_fact(route.memory_fact["label"], route.memory_fact["value"])
    evidence = select_memory_store_evidence(route)
    plan = build_memory_store_plan(route, evidence, saved_fact)
    return {
        "response": compose_controlled_response(plan),
        "saved_fact": saved_fact,
        "confidence": plan.confidence,
    }


def _resolve_memory_recall_response(query):
    route = _route_query(query)
    if route.intent != "memory_recall" or not route.recall_label:
        return None
    fact = _recall_chat_fact(route.recall_label)
    evidence = select_memory_recall_evidence(route, fact)
    plan = build_memory_recall_plan(route, evidence)
    return {
        "response": compose_controlled_response(plan),
        "fact": fact,
        "confidence": plan.confidence,
        "label": route.recall_label,
    }


def _retrieve_troubleshooting_chunks(query):
    raw_chunks = hybrid_retriever.query(
        query_text=query,
        n_policies=6,
        n_blogs=0,
        blog_min_relevance=1.0,
        format_for_llm=False,
    )
    cleaned = []
    for chunk in raw_chunks or []:
        source_collection = getattr(chunk, "source_collection", "")
        if source_collection not in {"policies", "fixes"}:
            continue
        content = hybrid_retriever._strip_section_labels(getattr(chunk, "content", ""))
        if not content:
            continue
        chunk.content = content
        cleaned.append(chunk)
    return cleaned


def _resolve_troubleshooting_response(query):
    route = _route_query(query)
    if route.intent != "troubleshooting":
        return None
    raw_chunks = _retrieve_troubleshooting_chunks(query)
    if _has_unsupported_specific_terms(query, raw_chunks):
        return {
            "response": _build_weak_evidence_fallback(query, route.intent, "low", raw_chunks),
            "confidence": "low",
            "sources_used": len(raw_chunks),
            "topic": route.topic,
        }
    evidence = select_troubleshooting_evidence(route, raw_chunks)
    plan = build_troubleshooting_plan(route, evidence)
    return {
        "response": compose_controlled_response(plan),
        "confidence": plan.confidence,
        "sources_used": len(evidence.evidence_blocks),
        "topic": plan.topic,
    }


def _retrieve_architecture_chunks(query):
    try:
        return hybrid_retriever.query(
            query_text=query,
            n_policies=6,
            n_blogs=4,
            blog_min_relevance=0.008,
            format_for_llm=False,
        )
    except Exception as e:
        logger.warning(f"[Architecture] Retrieval fallback triggered: {e}")
        return []


def _resolve_architecture_response(query):
    route = _route_query(query)
    if route.intent != "architecture":
        return None
    about = _get_about_data() if route.topic == "self_runtime" else None
    raw_chunks = [] if about else _retrieve_architecture_chunks(query)
    evidence = select_architecture_evidence(route, raw_chunks, about=about)
    plan = build_architecture_plan(route, evidence)
    return {
        "response": compose_controlled_response(plan),
        "confidence": plan.confidence,
        "sources_used": len(evidence.evidence_blocks),
        "topic": plan.topic,
        "response_mode": route.response_mode,
    }


def _retrieve_comparison_chunks(query):
    raw_chunks = hybrid_retriever.query(
        query_text=query,
        n_policies=4,
        n_blogs=2,
        blog_min_relevance=0.01,
        format_for_llm=False,
    )
    cleaned = []
    for chunk in raw_chunks or []:
        if hasattr(chunk, "content"):
            chunk.content = hybrid_retriever._strip_section_labels(getattr(chunk, "content", ""))
        cleaned.append(chunk)
    return cleaned


def _resolve_follow_up_response(query):
    route = _route_query(query)
    if route.intent != "follow_up":
        return None
    operational = _resolve_operational_follow_up_response(query)
    if operational:
        return operational
    recent_turns = _get_recent_distinct_turns(limit=4)
    evidence = select_follow_up_evidence(route, recent_turns, _topic_from_turn, _response_summary)
    plan = build_follow_up_plan(route, evidence)
    return {
        "response": compose_controlled_response(plan),
        "confidence": plan.confidence,
        "sources_used": len(recent_turns[:2]),
        "topic": plan.topic,
    }


_FOLLOW_UP_EXECUTION_MARKERS = (
    "do that", "do it", "run that", "run it", "apply that", "apply it",
    "fix it", "run the next step", "continue with that",
)

_FOLLOW_UP_NEXT_STEP_MARKERS = (
    "what should i do next", "what is the next step", "next step",
)


def _extract_action_after_label(text: str, labels: tuple[str, ...]) -> str:
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        for label in labels:
            prefix = f"- {label}:"
            if line.lower().startswith(prefix.lower()):
                return line[len(prefix):].strip()
    return ""


def _extract_follow_up_action(turn: dict) -> dict | None:
    response = _normalize_text((turn or {}).get("response", "")).strip()
    if not response:
        return None
    remediation = _extract_action_after_label(response, ("Action",))
    if remediation and "[Safest Remediation Path]" in response:
        return {
            "kind": "remediation",
            "action": remediation,
            "summary": "safest remediation path",
        }
    diagnostic = _extract_action_after_label(response, ("Step",))
    if diagnostic and "[Next Diagnostic Step]" in response:
        return {
            "kind": "diagnostic",
            "action": diagnostic,
            "summary": "next diagnostic step",
        }
    next_action = _extract_action_after_label(response, ("Next action",))
    if next_action and not next_action.lower().startswith("no urgent action"):
        return {
            "kind": "next_action",
            "action": next_action,
            "summary": "next action",
        }
    return None


def _get_recent_operational_turns(limit=6):
    rows = db.get_recent_queries(n=limit * 3)
    useful = []
    for row in reversed(rows):
        intent = _normalize_text(row.get("intent")).strip().lower()
        if intent in {"follow_up", "general", "definition", "comparison", "architecture", "ava_self"}:
            continue
        response = _normalize_text(row.get("response")).strip()
        if not response:
            continue
        turn = {
            "query": _normalize_text(row.get("query")).strip(),
            "response": response,
            "intent": intent,
        }
        if _extract_follow_up_action(turn):
            useful.append(turn)
        if len(useful) >= limit:
            break
    return useful


def _resolve_operational_follow_up_response(query):
    q = _normalize_text(query).lower().strip()
    wants_execution = any(marker in q for marker in _FOLLOW_UP_EXECUTION_MARKERS)
    wants_next_step = any(marker in q for marker in _FOLLOW_UP_NEXT_STEP_MARKERS)
    if not wants_execution and not wants_next_step:
        return None

    recent_turns = _get_recent_operational_turns(limit=4)
    if not recent_turns:
        return {
            "response": "I do not have a recent operational result with a concrete next action to continue from.",
            "confidence": "low",
            "sources_used": 0,
            "topic": "follow_up",
        }

    action_context = _extract_follow_up_action(recent_turns[0])
    if not action_context:
        return None
    action = action_context["action"]

    if wants_execution:
        direct_action = _resolve_direct_action_query(action)
        if direct_action and direct_action["kind"] == "command":
            return {
                "type": "command",
                "intent": "command",
                "response": direct_action["response"],
                "result": _build_command_response(direct_action["result"]),
                "raw_result": direct_action["result"],
                "sources_used": 1,
            }
        return {
            "response": (
                f"The last {action_context['summary']} was: {action}.\n\n"
                "I did not execute it because it does not resolve to a supported AVA action. "
                "Please restate the exact action if you want me to queue or run it."
            ),
            "confidence": "medium",
            "sources_used": 1,
            "topic": "follow_up",
        }

    return {
        "response": (
            f"The next grounded step from the last operational result is: {action}.\n\n"
            "If you want me to proceed, say `run that`. AVA will still apply the normal safety and approval checks."
        ),
        "confidence": "high",
        "sources_used": 1,
        "topic": "follow_up",
    }


def _resolve_comparison_response(query):
    route = _route_query(query)
    if route.intent != "comparison":
        return None
    evidence = select_comparison_evidence(route, _retrieve_comparison_chunks(query))
    plan = build_comparison_plan(route, evidence)
    return {
        "response": compose_controlled_response(plan),
        "confidence": plan.confidence,
        "sources_used": len(evidence.evidence_blocks),
        "topic": plan.topic,
    }


class _SeedKnowledgeChunk:
    def __init__(self, content, source_collection="policies"):
        self.content = content
        self.source_collection = source_collection


_CORE_DEVOPS_DEFINITION_BLOCKS = {
    "kubernetes": [
        "Kubernetes is a container orchestration platform that keeps containerized applications running in the desired state across a cluster.",
        "Kubernetes manages workloads through Pods, Deployments, Services, ConfigMaps, Secrets, volumes, scheduling, scaling, and rollout or rollback control.",
        "Operators use Kubernetes when applications need service discovery, self-healing, horizontal scaling, controlled releases, and consistent runtime management across multiple nodes.",
        "Kubernetes is not the same as Docker: Docker builds and runs containers, while Kubernetes coordinates many containers and the infrastructure objects around them.",
    ],
    "docker": [
        "Docker is a container runtime and image tooling platform used to package applications with their dependencies into portable container images.",
        "Docker runs containers from images, manages local container networking and volumes, and is commonly used for development, packaging, and single-host runtime workflows.",
        "In production platforms, Docker-style images are often scheduled by orchestrators such as Kubernetes rather than managed manually container by container.",
    ],
    "terraform": [
        "Terraform is an infrastructure-as-code tool that declares cloud and platform resources in configuration files and reconciles real infrastructure toward that desired state.",
        "Terraform plans changes before applying them, tracks managed resources in state, and is commonly used for repeatable provisioning across AWS, Azure, GCP, and other providers.",
        "Operators should review plans carefully because Terraform can create, modify, or destroy infrastructure depending on configuration and state.",
    ],
    "helm": [
        "Helm is a Kubernetes package manager that templates and installs related Kubernetes manifests as a versioned release.",
        "Helm charts package Deployments, Services, ConfigMaps, Secrets, and other resources so applications can be installed, upgraded, rolled back, and configured consistently.",
        "Operators should inspect rendered manifests and values because a chart upgrade can change live Kubernetes resources.",
    ],
    "linux": [
        "Linux is an operating system kernel and ecosystem commonly used to run servers, containers, networking, storage, and infrastructure services.",
        "For DevOps work, Linux is the runtime layer where processes, services, filesystems, users, permissions, packages, logs, and network sockets are inspected and controlled.",
        "Operational checks should be grounded in live host facts such as process state, service state, disk usage, package versions, and logs.",
    ],
    "pod": [
        "A Kubernetes Pod is the smallest schedulable workload unit in Kubernetes and runs one or more tightly related containers together.",
        "Pods share networking and storage context, so containers in the same Pod can communicate through localhost and share mounted volumes.",
        "Operators usually manage Pods through higher-level controllers such as Deployments, StatefulSets, DaemonSets, or Jobs rather than creating standalone Pods manually.",
    ],
    "deployment": [
        "A Kubernetes Deployment is a controller that manages stateless Pods through ReplicaSets and keeps the requested number of replicas running.",
        "Deployments support rolling updates, rollbacks, scaling, and declarative desired state for application workloads.",
        "Operators inspect Deployments when rollout status, replica count, image version, readiness, or application availability is wrong.",
    ],
    "service": [
        "A Kubernetes Service gives a stable virtual network endpoint for reaching a changing set of Pods selected by labels.",
        "Services decouple clients from individual Pod IPs and commonly expose workloads inside the cluster or through NodePort and LoadBalancer integrations.",
        "Operators check Services together with endpoints, selectors, readiness probes, DNS, and ingress rules when traffic is not reaching Pods.",
    ],
    "configmap": [
        "A Kubernetes ConfigMap is an object that stores non-secret configuration data so Pods can consume settings without baking them into container images.",
        "ConfigMaps can be mounted as files or exposed as environment variables, but they are not meant for passwords or credentials.",
        "Operators inspect ConfigMaps when configuration drift, missing keys, stale mounted values, or application startup failures are suspected.",
    ],
    "ingress": [
        "Kubernetes Ingress defines HTTP and HTTPS routing rules that send external traffic to Services inside a cluster.",
        "Ingress usually depends on an ingress controller such as NGINX, Traefik, or a cloud load-balancer integration to enforce those rules.",
        "Operators inspect Ingress with Services, endpoints, TLS secrets, DNS, controller logs, and backend readiness when external traffic fails.",
    ],
    "readiness probe": [
        "A readiness probe tells Kubernetes whether a container is ready to receive traffic.",
        "When readiness fails, Kubernetes removes the Pod from Service endpoints without necessarily restarting the container.",
        "Operators inspect readiness probes when Pods are running but traffic is not reaching them or rollouts never become available.",
    ],
    "liveness probe": [
        "A liveness probe tells Kubernetes whether a container should be restarted because it appears unhealthy.",
        "When liveness fails repeatedly, Kubernetes restarts the container, which can create restart loops if the probe is too strict or the app startup is slow.",
        "Operators inspect liveness probes when containers restart unexpectedly despite the application only being temporarily slow or overloaded.",
    ],
    "oomkilled": [
        "OOMKilled is a Kubernetes termination reason that means a container exceeded its memory limit and was killed by the kernel.",
        "Operators investigate OOMKilled events by checking memory limits, peak usage, application leaks, recent deployments, and node memory pressure.",
        "The safe fix is not always increasing memory; first confirm whether the workload is undersized, leaking memory, or receiving abnormal traffic.",
    ],
    "crashloopbackoff": [
        "CrashLoopBackOff means Kubernetes is repeatedly starting a container, seeing it exit or fail, and backing off before retrying.",
        "Common causes include bad entrypoints, missing configuration, failing probes, image issues, permission errors, or dependency failures.",
        "Operators investigate CrashLoopBackOff with `kubectl describe pod`, current and previous logs, events, mounts, secrets, config, and recent rollout changes.",
    ],
    "namespace": [
        "A Kubernetes namespace is a logical scope for grouping and isolating namespaced resources inside a cluster.",
        "Namespaces help separate teams, environments, permissions, quotas, policies, and resource names without creating separate clusters.",
        "Operators include namespace context when checking Pods, Services, events, secrets, RBAC, quotas, and network policies.",
    ],
    "pvc": [
        "A PersistentVolumeClaim is a Kubernetes request for durable storage that a Pod can mount through a PersistentVolume.",
        "PVCs decouple workload manifests from the underlying storage implementation such as cloud disks, network storage, or local volumes.",
        "Operators inspect PVCs when Pods are stuck pending, mounts fail, storage is full, access modes mismatch, or a storage class cannot provision a volume.",
    ],
    "dockerfile": [
        "A Dockerfile is a build recipe that defines how to create a container image from a base image, files, dependencies, environment, and commands.",
        "Operators review Dockerfiles for reproducibility, image size, build caching, secret leakage, package freshness, user privileges, and runtime command behavior.",
        "A good Dockerfile keeps build-time concerns separate from runtime behavior and avoids baking credentials or host-specific assumptions into the image.",
    ],
    "kubeconfig": [
        "A kubeconfig file is a Kubernetes client configuration file that stores cluster connection details, users, contexts, and credentials used by kubectl and clients.",
        "Operators use kubeconfig contexts to choose which cluster and namespace kubectl commands target.",
        "Kubeconfig files are sensitive because they may contain credentials or token references, so access should be controlled and audited.",
    ],
}


def _core_definition_terms(query):
    q = (query or "").lower().strip()
    terms = []
    for term in _CORE_DEVOPS_DEFINITION_BLOCKS:
        if re.search(rf"\b{re.escape(term)}\b", q) or (term == "kubernetes" and re.search(r"\bk8s\b", q)):
            terms.append(term)
    return terms


def _seed_definition_chunks(query):
    chunks = []
    seen_lines = set()
    for term in _core_definition_terms(query):
        lines = _CORE_DEVOPS_DEFINITION_BLOCKS.get(term, [])
        block = "\n".join(line for line in lines if line and line not in seen_lines)
        if block:
            chunks.append(_SeedKnowledgeChunk(block, source_collection="seeded_definitions"))
            seen_lines.update(lines)
    return chunks


def _retrieve_definition_chunks(query):
    seed_chunks = _seed_definition_chunks(query)
    raw_chunks = hybrid_retriever.query(
        query_text=query,
        n_policies=6,
        n_blogs=2,
        blog_min_relevance=0.01,
        format_for_llm=False,
    )
    entities = _extract_query_entities(query)
    boosted_chunks = []
    if entities and hasattr(hybrid_retriever, "_keyword_fetch"):
        keyword_limit = 4
        for collection in [
            getattr(hybrid_retriever, "patterns_collection", None),
            getattr(hybrid_retriever, "policies_collection", None),
        ]:
            boosted_chunks.extend(hybrid_retriever._keyword_fetch(collection, entities, limit=keyword_limit))
    if boosted_chunks:
        seen = {getattr(chunk, "content", None) for chunk in (raw_chunks or [])}
        prepended = []
        for chunk in boosted_chunks:
            content = getattr(chunk, "content", None)
            if content not in seen:
                prepended.append(chunk)
                seen.add(content)
        if prepended:
            raw_chunks = prepended + list(raw_chunks or [])
    if seed_chunks:
        raw_chunks = seed_chunks + list(raw_chunks or [])
    cleaned = []
    for chunk in raw_chunks or []:
        if hasattr(chunk, "content"):
            chunk.content = hybrid_retriever._strip_section_labels(getattr(chunk, "content", ""))
        cleaned.append(chunk)
    return cleaned


def _resolve_definition_response(query):
    route = _route_query(query)
    if route.intent != "definition":
        return None
    raw_chunks = _retrieve_definition_chunks(query)
    if _has_unsupported_specific_terms(query, raw_chunks):
        return {
            "response": _build_weak_evidence_fallback(query, route.intent, "low", raw_chunks),
            "confidence": "low",
            "sources_used": len(raw_chunks),
            "topic": route.topic,
        }
    evidence = select_definition_evidence(route, raw_chunks)
    plan = build_definition_plan(route, evidence)
    return {
        "response": compose_controlled_response(plan),
        "confidence": plan.confidence,
        "sources_used": len(evidence.evidence_blocks),
        "topic": plan.topic,
    }

def _chat_payload(response_text="", response_type="knowledge", ok=True, confidence=None,
                  sources_used=0, time_taken="", **extra):
    payload = {
        "ok": ok,
        "type": response_type,
        "response": _normalize_text(response_text),
        "sources_used": sources_used or 0,
        "time_taken": time_taken or "",
    }
    if confidence is not None:
        payload["confidence"] = confidence
    payload.update(extra)
    return payload

def _record_query(query, response, intent, elapsed, sources_used=0, confidence=None):
    response_text = _normalize_text(response)
    save_history({
        'timestamp': datetime.now().isoformat(),
        'query': query,
        'type': intent,
        'sources_used': sources_used,
        'time_taken': f"{elapsed:.2f}s",
        'response_preview': response_text[:200] + '...' if len(response_text) > 200 else response_text,
    })
    try:
        clean_response = response_text
        for prefix in _CONFIDENCE_PREFIXES.values():
            if clean_response.startswith(prefix):
                clean_response = clean_response[len(prefix):]
                break
        db.save_query(
            query=query,
            response=clean_response,
            confidence=confidence,
            intent=intent,
            sources_used=sources_used,
        )
    except Exception as _dbe:
        logger.warning(f"[DB] save_query failed: {_dbe}")


def _resolve_controlled_query(query, *, controlled_route=None, prior_messages=None):
    controlled_route = controlled_route or _route_query(query)

    direct_action = _resolve_direct_action_query(query)
    if direct_action:
        if direct_action["kind"] == "command":
            return {
                "type": "command",
                "intent": "command",
                "response": direct_action["response"],
                "result": _build_command_response(direct_action["result"]),
                "raw_result": direct_action["result"],
                "sources_used": 0,
            }
        return {
            "type": "knowledge",
            "intent": "knowledge",
            "response": direct_action["response"],
            "confidence": direct_action.get("confidence", "high"),
            "sources_used": 0,
        }

    if controlled_route.intent == "troubleshooting":
        resolved = _resolve_troubleshooting_response(query)
        return {
            "type": "knowledge",
            "intent": "troubleshooting",
            "response": resolved["response"],
            "confidence": resolved["confidence"],
            "sources_used": resolved["sources_used"],
        }

    if controlled_route.intent == "architecture":
        resolved = _resolve_architecture_response(query)
        return {
            "type": "diagram" if resolved["response_mode"] == "diagram" else "knowledge",
            "intent": "architecture",
            "response": resolved["response"],
            "confidence": resolved["confidence"],
            "sources_used": resolved["sources_used"],
        }

    if controlled_route.intent == "follow_up":
        resolved = _resolve_follow_up_response(query)
        if resolved.get("type") == "command":
            return resolved
        return {
            "type": "knowledge",
            "intent": "follow_up",
            "response": resolved["response"],
            "confidence": resolved["confidence"],
            "sources_used": resolved["sources_used"],
        }

    if controlled_route.intent == "comparison":
        resolved = _resolve_comparison_response(query)
        return {
            "type": "knowledge",
            "intent": "comparison",
            "response": resolved["response"],
            "confidence": resolved["confidence"],
            "sources_used": resolved["sources_used"],
        }

    if controlled_route.intent == "definition":
        resolved = _resolve_definition_response(query)
        return {
            "type": "knowledge",
            "intent": "definition",
            "response": resolved["response"],
            "confidence": resolved["confidence"],
            "sources_used": resolved["sources_used"],
        }

    if _is_healing_query(query) or detect_query_intent(query) == "healing_incident":
        response, healing_meta = _build_healing_response(query)
        return {
            "type": "healing",
            "intent": "healing_incident",
            "response": response,
            "confidence": "high",
            "sources_used": 0,
            "healing": healing_meta,
            "action_taken": healing_meta.get("action_taken"),
        }

    if is_greeting(query):
        return {
            "type": "knowledge",
            "intent": "greeting",
            "response": "Hello! I'm AVA, your local DevOps AI assistant. How can I help you today with infrastructure, containers, or cloud services?",
            "sources_used": 0,
        }

    if controlled_route.intent == "ava_self":
        return {
            "type": "knowledge",
            "intent": "ava_self",
            "response": _resolve_ava_self_response(query),
            "confidence": "high",
            "sources_used": 0,
        }

    if _should_direct_unknown_to_llm(query, route=controlled_route) and not looks_like_operational_request(query):
        resolved = _resolve_general_unknown_response(query, prior_messages=prior_messages, route=controlled_route)
        return {
            "type": "knowledge",
            "intent": "general",
            "response": resolved["response"],
            "confidence": resolved["confidence"],
            "sources_used": resolved["sources_used"],
        }

    return None


def _resolve_grounded_knowledge_query(query, *, prior_messages=None):
    query_intent = detect_query_intent(query)
    context = query_knowledge_base(query, query_intent=query_intent)
    confidence = score_context_confidence(context, query)
    confidence = _apply_confidence_rules(confidence, context, query)

    if _should_use_weak_evidence_fallback(query, query_intent, confidence, context):
        response = _build_weak_evidence_fallback(query, query_intent, confidence, context)
        return {
            "type": "knowledge",
            "intent": "knowledge",
            "response": response,
            "confidence": "low",
            "sources_used": len(context),
            "context": context,
            "query_intent": query_intent,
        }

    response = generate_response(query, context, confidence=confidence, prior_messages=prior_messages)
    if query_intent != "healing_incident" and _looks_like_invalid_json_wrapper(response):
        response = _repair_definition_wrapper(response)

    if context and len(context) < 2 and is_weak_response(response):
        logger.info("[FAILSAFE] Weak response - retrying with extended retrieval...")
        retry_chunks = hybrid_retriever.query(
            query_text=query,
            n_policies=6,
            n_blogs=10,
            blog_min_relevance=0.001,
            format_for_llm=False
        )
        if retry_chunks:
            retry_context = hybrid_retriever.assemble_context(
                retry_chunks, max_articles=5, max_chunks_per_article=4
            )
            retry_confidence = score_context_confidence(retry_context, query)
            retry_confidence = _apply_confidence_rules(retry_confidence, retry_context, query)
            response = generate_response(query, retry_context, confidence=retry_confidence, prior_messages=prior_messages)
            if query_intent != "healing_incident" and _looks_like_invalid_json_wrapper(response):
                response = _repair_definition_wrapper(response)
            context = retry_context
            confidence = retry_confidence
            logger.info("[FAILSAFE] Retry complete")

    if _should_use_weak_evidence_fallback(query, query_intent, confidence, context, response=response):
        response = _build_weak_evidence_fallback(query, query_intent, confidence, context)
        confidence = "low"

    if is_technical_query(query) and not is_weak_response(response):
        update_memory_issue(query, response[:200])
        logger.info("[MEMORY] Issue saved to ava_memory.json")

    return {
        "type": "knowledge",
        "intent": "knowledge",
        "response": response,
        "confidence": confidence,
        "sources_used": len(context),
        "context": context,
        "query_intent": query_intent,
    }


def _finalize_command_payload(query, raw_result, response_text, elapsed):
    response_text = _normalize_text(response_text)
    save_history({
        'timestamp': datetime.now().isoformat(),
        'query': query,
        'type': 'command',
        'blocked': raw_result.get('status') == 'blocked',
        'time_taken': f"{elapsed:.2f}s",
        'response_preview': response_text[:200] + '...' if len(response_text) > 200 else response_text,
    })
    try:
        db.save_query(
            query=query,
            response=response_text,
            confidence=raw_result.get("risk") or raw_result.get("status"),
            intent="command",
            sources_used=0,
        )
    except Exception as _dbe:
        logger.warning(f"[DB] save command query failed: {_dbe}")
    return _chat_payload(
        response_text,
        response_type='command',
        time_taken=f"{elapsed:.2f}s",
        result=_build_command_response(raw_result),
    )


def _finalize_resolved_payload(query, resolved, elapsed):
    if resolved["type"] == "command":
        return _finalize_command_payload(query, resolved["raw_result"], resolved["response"], elapsed)

    _record_query(
        query,
        resolved["response"],
        resolved["intent"],
        elapsed,
        sources_used=resolved.get("sources_used", 0),
        confidence=resolved.get("confidence"),
    )
    return _chat_payload(
        resolved["response"],
        response_type=resolved["type"],
        confidence=resolved.get("confidence"),
        sources_used=resolved.get("sources_used", 0),
        time_taken=f"{elapsed:.2f}s",
        **{
            key: resolved[key]
            for key in ("healing", "action_taken", "graph_used", "steps_run", "react_trace")
            if key in resolved
        }
    )

def _normalize_fact_key(label):
    return re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')

def _canonical_fact_label(label):
    normalized = re.sub(r'\s+', ' ', _normalize_text(label).strip().lower())
    aliases = {
        "server": "server name",
        "cluster": "cluster name",
        "project": "project name",
        "service": "service name",
        "app": "application name",
        "application": "application name",
    }
    return aliases.get(normalized, normalized)

def _fact_aliases(label):
    base = _normalize_fact_key(_canonical_fact_label(label))
    aliases = {base}
    if not base:
        return aliases
    simplified = re.sub(r'_(name|id|value|details?)$', '', base)
    if simplified:
        aliases.add(simplified)
        aliases.add(f"{simplified}_name")
        aliases.add(f"{simplified}_id")
    aliases.add(base.replace("_name", ""))
    aliases.add(base.replace("_id", ""))
    return {alias.strip("_") for alias in aliases if alias.strip("_")}

def _extract_query_entities(query):
    query = _normalize_text(query)
    lower_query = query.lower()
    tech_terms = sorted({
        *_INFRA_COMPONENTS,
        *_KNOWN_DIAGRAM_TECH,
        "crashloopbackoff", "oomkilled", "readiness probe", "liveness probe",
        "api gateway", "load balancer",
    }, key=len, reverse=True)

    entities = []
    seen = set()

    def add_entity(value):
        cleaned = _normalize_text(value).strip(' .,:;`"\'')
        lower = cleaned.lower()
        if not cleaned or lower in _ENTITY_STOP_WORDS or len(lower) < 2:
            return
        if lower not in seen:
            seen.add(lower)
            entities.append(cleaned)

    for match in re.findall(r'[`"\']([^`"\']{2,60})[`"\']', query):
        add_entity(match)

    for term in tech_terms:
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lower_query):
            add_entity(term)

    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_\-:/+.]{2,}", query)
    important_tokens = {
        "kafka", "zuul", "cassandra", "evcache", "nginx", "kubernetes",
        "docker", "redis", "postgres", "vault", "opa", "oomkilled",
        "crashloopbackoff", "readiness", "liveness", "probe", "samza",
        "mantis", "netty", "eureka", "hystrix", "argocd", "grafana",
        "prometheus", "terraform", "jenkins", "consul", "envoy", "istio",
    }
    noisy_terms = {
        "diagram", "architecture", "request", "flow", "data", "component",
        "components", "sequence", "topology", "question", "difference",
        "previous", "thing", "asked", "latest", "recent", "compare",
        "create", "draw", "make", "render", "explain",
    }
    for token in tokens:
        lower = token.lower()
        if lower in _ENTITY_STOP_WORDS or lower in noisy_terms:
            continue
        if (
            any(ch.isupper() for ch in token)
            or "-" in token
            or "/" in token
            or any(ch.isdigit() for ch in token)
            or lower in important_tokens
        ):
            add_entity(token)

    return entities[:10]

def _diagram_entities_from_text(*texts):
    combined = "\n".join(_normalize_text(text) for text in texts if text)
    return _extract_query_entities(combined)

_KNOWN_DIAGRAM_TECH = [
    "zuul", "kafka", "cassandra", "evcache", "eureka", "ribbon",
    "hystrix", "resilience4j", "samza", "mantis", "netty",
    "elasticsearch", "spark", "s3", "dynamodb", "sqs", "sns",
    "redis", "mysql", "postgres", "mongodb", "nginx", "consul",
    "vault", "prometheus", "grafana", "jaeger", "istio", "envoy",
    "docker", "kubernetes", "terraform", "jenkins", "argocd",
    "api gateway", "load balancer", "cdn", "open connect",
]

def _extract_diagram_entities(llava_text: str) -> list:
    """Extract technology names from llava analysis output."""
    found = []
    text_lower = llava_text.lower()
    for tech in _KNOWN_DIAGRAM_TECH:
        if tech in text_lower:
            found.append(tech)
    return found

def _build_diagram_grounding_block(question, context_blocks, extracted_entities):
    lines = [
        "Diagram grounding rules:",
        "- Use only components visible in the diagram analysis or supported by retrieved context.",
        "- Do not introduce technologies that are not grounded.",
        "- Explain request flow and data flow only when grounded by the input/context.",
        "- Preserve the exact grounded component names in the final answer.",
    ]
    if extracted_entities:
        lines.append("Diagram entities detected: " + ", ".join(extracted_entities))
    if context_blocks:
        lines.append("Grounded diagram facts:")
        relevant = _extract_relevant_context_lines(context_blocks, extracted_entities, limit=10)
        lines.extend(f"- {line}" for line in relevant[:10])
    return "\n".join(lines)


def _is_ava_self_architecture_query(query, entities=None):
    q = (query or "").lower()
    markers = [
        "your docker", "your architecture", "your containers", "your services",
        "your ports", "your stack", "ava", "your runtime",
    ]
    if any(marker in q for marker in markers):
        return True
    for entity in entities or []:
        if entity.lower() in {
            "ava-agent", "flask/gunicorn", "postgresql", "redis",
            "open policy agent", "hashicorp vault", "ollama host",
        }:
            return True
    return False


def _needs_strict_grounding(query_intent, query):
    if query_intent in {"ava_self", "healing_incident", "follow_up", "memory_recall", "memory_store", "architecture"}:
        return True
    q = query.lower()
    return any(term in q for term in ["diagram", "architecture", "request flow", "data flow", "components"])

def _extract_relevant_context_lines(context_blocks, entities, limit=8):
    if not context_blocks:
        return []

    relation_terms = {
        "handles", "routes", "calls", "uses", "writes", "stores", "publishes",
        "sends", "reads", "connects", "proxies", "feeds", "triggers",
        "loads", "carries", "behind", "through", "via",
    }

    if not entities:
        lines = []
        for block in context_blocks[:3]:
            for line in block.splitlines():
                cleaned = line.strip(" -\t")
                if cleaned:
                    lines.append(cleaned)
                if len(lines) >= limit:
                    return lines
        return lines

    scored = []
    for block in context_blocks:
        for line in block.splitlines():
            cleaned = line.strip(" -\t")
            if not cleaned:
                continue
            if _is_noisy_architecture_line(cleaned):
                continue
            lower = cleaned.lower()
            match_count = sum(1 for entity in entities if entity.lower() in lower)
            if not match_count:
                continue
            relation_score = sum(1 for term in relation_terms if term in lower)
            scored.append((match_count, relation_score, len(cleaned), cleaned))

    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    matches = []
    seen = set()
    for _, _, _, cleaned in scored:
        lower = cleaned.lower()
        if lower in seen:
            continue
        seen.add(lower)
        matches.append(cleaned)
        if len(matches) >= limit:
            break
    return matches


def _is_noisy_architecture_line(line):
    lower = _normalize_text(line).strip().lower()
    if not lower:
        return True
    noise_markers = (
        "entities detected:",
        "diagram grounding rules:",
        "grounded diagram facts:",
        "terraform init",
        "architecture reference:",
        "queue depth",
        "consumer lag",
        "redis_blocked_clients",
        "kafka_consumer_lag",
        "slo:",
        "example:",
        "alert:",
        "alerting example",
        "30-day rolling",
    )
    if lower.startswith("#"):
        return True
    if any(marker in lower for marker in noise_markers):
        return True
    if re.search(r"\b[a-z]+_[a-z0-9_]+\b", lower) and not any(term in lower for term in ("gateway", "stream", "cache", "store", "event", "request", "route", "process", "monitor")):
        return True
    return False


def _build_grounding_block(query, context_blocks, query_intent):
    entities = _extract_query_entities(query)
    matched_lines = _extract_relevant_context_lines(context_blocks, entities)
    lines = [f"Grounded intent: {query_intent}"]
    if entities:
        lines.append("Entities detected: " + ", ".join(entities))
    if matched_lines:
        lines.append("Relevant facts from context:")
        lines.extend(f"- {line}" for line in matched_lines[:8])
    else:
        lines.append("Relevant facts from context: none confidently matched.")
    return "\n".join(lines)

def _grounding_confident_enough(query, context_blocks, confidence):
    if confidence == "high":
        return True
    entities = _extract_query_entities(query)
    matched_lines = _extract_relevant_context_lines(context_blocks, entities)
    q = query.lower()
    if any(term in q for term in ["diagram", "architecture", "request flow", "data flow", "components"]):
        relation_terms = ("route", "request", "gateway", "stream", "event", "cache", "store", "read", "write", "monitor", "process", "carry")
        relation_line_hits = 0
        for block in context_blocks or []:
            for line in block.splitlines():
                lower = line.lower()
                entity_hits = sum(1 for entity in entities if entity.lower() in lower)
                if entity_hits >= 1 and any(term in lower for term in relation_terms):
                    relation_line_hits += 1
        return bool(matched_lines) or (confidence == "medium" and relation_line_hits >= 1 and len(entities) >= 3)
    if not entities:
        return confidence in {"medium", "high"} and bool(context_blocks)
    return len(matched_lines) >= 2 or (confidence == "medium" and len(matched_lines) >= 1 and len(entities) <= 3)

def _load_chat_facts():
    return db.get_memory(_MEMORY_FACT_KEY, {}) or {}

def _save_chat_fact(label, value):
    facts = _load_chat_facts()
    label = _canonical_fact_label(label)
    key = _normalize_fact_key(label)
    facts[key] = {
        "label": label,
        "value": value.strip(),
        "updated_at": datetime.now().isoformat(),
    }
    db.save_memory(_MEMORY_FACT_KEY, facts)
    return facts[key]

def _parse_memory_statement(statement):
    text = statement.strip().strip(". ")
    if not text:
        return None
    if "=" in text:
        left, right = text.split("=", 1)
        label = left.strip().replace("_", " ")
        value = right.strip().strip(". ")
        if label and value:
            return {"label": label, "value": value}
    m = re.match(r"(?:my\s+)?(.+?)\s+is\s+(.+)", text, re.IGNORECASE)
    if m:
        label = m.group(1).strip()
        value = m.group(2).strip().strip(". ")
        if label and value:
            return {"label": label, "value": value}
    return None

def _extract_memory_request(query):
    query = _normalize_user_query(query)
    m = re.match(
        r"\s*remember(?: this)?(?: exactly)?\s*:\s*(.+?)(?:[.?!]\s*(what.+))?\s*$",
        query,
        re.IGNORECASE,
    )
    if not m:
        return None
    fact = _parse_memory_statement(m.group(1))
    if not fact:
        return None
    follow_up = (m.group(2) or "").strip()
    return {"fact": fact, "follow_up": follow_up}

def _extract_recall_label(query):
    query = _normalize_user_query(query)
    patterns = [
        r"what is my (.+?)[\?]?$",
        r"what's my (.+?)[\?]?$",
        r"what (.+?) did i just mention[\?]?$",
        r"what did i just mention about my (.+?)[\?]?$",
    ]
    q = query.strip().lower()
    for pattern in patterns:
        m = re.match(pattern, q, re.IGNORECASE)
        if m:
            label = m.group(1).strip().replace("_", " ")
            return _canonical_fact_label(label)
    return None

def _recall_chat_fact(label):
    if not label:
        return None
    facts = _load_chat_facts()
    wanted_aliases = _fact_aliases(label)
    for alias in wanted_aliases:
        if alias in facts:
            return facts[alias]
    for fact in facts.values():
        stored_label = fact.get("label", "")
        stored_aliases = _fact_aliases(stored_label)
        if wanted_aliases & stored_aliases:
            return fact
        if label.lower() == stored_label.lower() or label.lower() in stored_label.lower():
            return fact
    return None

def _get_recent_prior_messages(n=4):
    recent = db.get_recent_queries(n=n)
    prior_messages = []
    for row in recent:
        prior_messages.append({"role": "user", "content": _normalize_text(row.get("query"))})
        prior_messages.append({"role": "assistant", "content": _normalize_text(row.get("response"))})
    return prior_messages

def _topic_signature(turn):
    topic = _normalize_text(_topic_from_turn(turn)).lower()
    return re.sub(r'[^a-z0-9]+', ' ', topic).strip()

def _get_recent_distinct_turns(limit=6):
    rows = db.get_recent_queries(n=limit * 3)
    useful = []
    seen_signatures = set()
    for row in reversed(rows):
        query = _normalize_text(row.get("query")).strip()
        if not query:
            continue
        intent = _normalize_text(row.get("intent")).strip().lower()
        if intent == "follow_up":
            continue
        turn = {
            "query": query,
            "response": _normalize_text(row.get("response")).strip(),
            "intent": intent,
        }
        signature = _topic_signature(turn)
        if signature and signature in seen_signatures:
            continue
        if signature:
            seen_signatures.add(signature)
        useful.append(turn)
        if len(useful) >= limit:
            break
    return list(reversed(useful))

def _summarize_topic(query):
    cleaned = _normalize_text(query).strip().rstrip("?.!")
    if not cleaned:
        return "an earlier topic"
    entities = _extract_query_entities(cleaned)
    if entities:
        if len(entities) == 1:
            return entities[0]
        if len(entities) == 2:
            return f"{entities[0]} and {entities[1]}"
        return ", ".join(entities[:3])
    cleaned = re.sub(r"^(what is|what are|how is|how does|how do|explain|describe|tell me about|show me)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned[0].lower() + cleaned[1:] if len(cleaned) > 1 else cleaned.lower()

def _topic_from_turn(turn):
    query = _normalize_text(turn.get("query"))
    response = _normalize_text(turn.get("response"))
    intent = _normalize_text(turn.get("intent")).lower()
    if intent == "architecture" or "```mermaid" in response:
        if _is_ava_self_architecture_query(query):
            return "ava-agent, PostgreSQL, Redis"
        cleaned_response = re.sub(r"```mermaid.*?```", "", response, flags=re.S).strip()
        entities = _diagram_entities_from_text(query, cleaned_response)
        if entities:
            return ", ".join(entities[:3])
    return _summarize_topic(query)

def _response_summary(response_text):
    response_text = _normalize_text(response_text).strip()
    if not response_text:
        return ""
    low_signal_phrases = {
        "the diagram shows the components of the docker architecture",
        "this diagram represents the interconnections between the main components",
    }
    if "```mermaid" in response_text:
        cleaned = re.sub(r"```mermaid.*?```", "", response_text, flags=re.S).strip()
        if cleaned:
            for line in cleaned.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    continue
                if re.fullmatch(r"\*\*[^*]+:\*\*", line):
                    continue
                if re.fullmatch(r"[A-Za-z][A-Za-z ]+:", line):
                    continue
                if line.startswith("- **") and ":**" in line:
                    continue
                if line.startswith("**") and line.endswith("**"):
                    continue
                normalized = re.sub(r"^[\-\*\s]+", "", line).strip().rstrip(".").lower()
                if normalized in low_signal_phrases:
                    continue
                return line
        return "A grounded Mermaid diagram of the architecture."
    for line in response_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"\*\*[^*]+:\*\*", line):
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z ]+:", line):
            continue
        normalized = re.sub(r"^[\-\*\s]+", "", line).strip().rstrip(".").lower()
        if normalized in low_signal_phrases:
            continue
        return line
    return response_text.split("\n", 1)[0].strip()

def _looks_like_invalid_json_wrapper(response_text):
    text = _normalize_text(response_text).strip()
    if not text.startswith("{") or '"issue_type"' not in text:
        return False
    try:
        parsed = json.loads(text)
    except Exception:
        return False
    return (
        parsed.get("issue_type") == "definition"
        and "action_taken" in parsed
        and "command" in parsed
    )

def _repair_definition_wrapper(response_text):
    text = _normalize_text(response_text).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return text
    action_taken = _normalize_text(parsed.get("action_taken")).strip()
    if action_taken:
        return action_taken
    return text

def _json_only_requested(query):
    q = query.lower()
    return "respond only in this json format" in q or "return valid json only" in q or "json only" in q

def _is_healing_query(query):
    q = query.lower()
    incident_terms = [
        "crashloopbackoff", "oomkilled", "oom killed", "disk full", "disk usage",
        "service is down", "service down", "imagepullbackoff", "image pull error",
        "node not ready", "cert expiry", "certificate expiry", "95% full",
    ]
    action_terms = [
        "healing action", "what should you do", "what would you do",
        "classify the issue type", "auto-execute or queue", "dry-run",
        "dry run", "respond only in this json format",
    ]
    state_terms = [" is down", " is 95%", " is in ", " would you take", " rollback", " risk level"]
    return any(term in q for term in incident_terms) and (
        any(term in q for term in action_terms) or any(term in q for term in state_terms)
    )

def _predict_heal_action(confidence, risk_level):
    if confidence >= 0.85 and risk_level == "LOW":
        return "auto_execute"
    if confidence >= 0.6:
        return "queued_for_approval"
    return "incident_logged"

def _format_playbook_template(template, entities):
    if not template:
        return None
    try:
        return template.format(**(entities or {}))
    except Exception:
        return template

def _build_healing_response(query):
    issue = healer.detect_issue(source="chat", message=query)
    playbook = healer.get_healing_action(issue.get("issue_type", ""))
    entities = issue.get("entities", {})
    command = _format_playbook_template(playbook.get("command"), entities) if playbook else None
    rollback = _format_playbook_template(playbook.get("rollback"), entities) if playbook else None
    risk_level = playbook.get("risk_level", "UNKNOWN") if playbook else "UNKNOWN"
    action_taken = _predict_heal_action(float(issue.get("confidence", 0.0)), risk_level)

    body = {
        "issue_type": issue.get("issue_type", "unknown"),
        "confidence": round(float(issue.get("confidence", 0.0)), 2),
        "command": command or "",
        "risk_level": risk_level,
        "rollback": rollback or "None",
        "action_taken": action_taken,
    }

    if _json_only_requested(query):
        response_text = json.dumps({
            "issue_type": body["issue_type"],
            "command": body["command"],
            "risk_level": body["risk_level"],
            "rollback": body["rollback"],
            "action_taken": body["action_taken"],
        }, ensure_ascii=False)
    else:
        response_text = (
            f"Issue Type: {body['issue_type']}\n"
            f"Confidence: {body['confidence']}\n"
            f"Command: {body['command'] or 'No command available'}\n"
            f"Risk Level: {body['risk_level']}\n"
            f"Rollback: {body['rollback']}\n"
            f"Action Taken: {body['action_taken']}"
        )

    return response_text, body

def detect_query_intent(query):
    q = _normalize_user_query(query).lower().strip()
    controlled_route = _route_query(query)
    if controlled_route.intent in ("ava_self", "memory_store", "memory_recall", "troubleshooting", "architecture", "follow_up", "comparison", "definition", "general_qwen"):
        return controlled_route.intent
    if _is_healing_query(q):
        return "healing_incident"
    # Metric-alert patterns not caught by _is_healing_query compound check
    _alert_terms = [
        "disk usage", "disk is", "disk at",
        "cpu usage", "cpu is at", "memory usage",
        "% full", "% usage", "% disk",
        "worker-", "node-",
    ]
    if any(k in q for k in _alert_terms):
        return "healing_incident"
    if re.search(r'\d+\s*%', q) and any(k in q for k in ["disk", "cpu", "memory", "usage", "full"]):
        return "healing_incident"
    return "general"

def build_memory_context(query=None):
    if not AVA_MEMORY:
        return ""
    infra = AVA_MEMORY.get("infra", {})
    active_tools = [k for k, v in infra.items() if v]
    user = AVA_MEMORY.get("user", "the user")
    prefs = AVA_MEMORY.get("preferences", {})
    lines = [f"User: {user}"]
    if active_tools:
        lines.append(f"Their infrastructure stack: {', '.join(active_tools)}")
    if prefs.get("style"):
        lines.append(f"Response style: {prefs['style']}")
    if query:
        past_issues = AVA_MEMORY.get("past_issues", [])
        if past_issues:
            query_words = {w.lower() for w in query.split() if len(w) >= 4}
            matches = [
                issue for issue in past_issues
                if any(w in issue.get("query", "").lower() for w in query_words)
            ]
            if matches:
                recent = matches[-3:]
                lines.append("Past fixes for similar issues:")
                for issue in recent:
                    lines.append(f"- {issue['query']}: {issue['resolution']}")
    return "\n".join(lines)

def force_knowledge_routing(query):
    q = query.lower().strip()
    knowledge_patterns = [
        "how to", "how do i", "how does", "how can i", "how should",
        "why is", "why does", "why did", "why would", "why are",
        "what is", "what are", "what does", "what should", "what causes",
        "debug", "fix", "troubleshoot", "resolve", "solve", "help me",
        "not working", "fails", "error", "issue", "problem", "broken",
        "crash", "stuck", "slow", "explain", "describe", "tell me",
        "best practice", "best way", "difference between", "compare",
        "architecture", "design", "diagram", "flow", "show me",
        "create a", "draw a", "visualize",
    ]
    for pattern in knowledge_patterns:
        if pattern in q:
            return True
    question_starters = ["how", "why", "what", "when", "where", "which", "explain", "describe", "create", "show"]
    first_word = q.split()[0] if q.split() else ""
    if first_word in question_starters:
        return True
    return False

_CONFIDENCE_STOP_WORDS = {
    'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'on',
    'at', 'by', 'for', 'with', 'about', 'from', 'into', 'through',
    'how', 'what', 'when', 'where', 'why', 'which', 'who', 'i', 'my',
    'your', 'it', 'its', 'this', 'that', 'and', 'or', 'not', 'if',
    'me', 'you', 'we', 'they', 'he', 'she', 'us', 'them',
    'computing', 'using', 'based', 'system', 'systems',
}

def score_context_confidence(context, query):
    """Phase 4.5: Score how well retrieved context matches the query.

    Returns 'high', 'medium', or 'low':
    - high:   2+ chunks with direct keyword overlap ratio > 0.3,
              AND the most specific token (longest non-stop word) appears
              in at least 2 chunks. Missing anchor token caps result at 'medium'.
    - medium: some overlap (ratio 0.1–0.3) or exactly 1 high chunk
    - low:    weak/no keyword overlap or no chunks
    """
    if not context:
        return 'low'

    query_tokens = set(
        w.lower().strip('.,?!:;') for w in query.split()
        if len(w) > 2 and w.lower().strip('.,?!:;') not in _CONFIDENCE_STOP_WORDS
    )
    if not query_tokens:
        return 'low'

    # Anchor token: longest query keyword — most likely to be domain-specific
    anchor_token = max(query_tokens, key=len)

    high_overlap_count = 0
    any_overlap = False
    anchor_chunk_count = 0

    for chunk in context:
        chunk_lower = chunk.lower()
        matched = sum(1 for tok in query_tokens if tok in chunk_lower)
        ratio = matched / len(query_tokens)
        if ratio > 0.3:
            high_overlap_count += 1
            any_overlap = True
        elif ratio >= 0.1:
            any_overlap = True
        if anchor_token in chunk_lower:
            anchor_chunk_count += 1

    if high_overlap_count >= 2 and anchor_chunk_count >= 2:
        return 'high'
    if any_overlap or high_overlap_count >= 1:
        return 'medium'
    return 'low'


def _apply_confidence_rules(confidence: str, context: list, query: str) -> str:
    """FIX 2: Rule-based confidence override — more reliable than LLM guess."""

    # Rule 1: No context at all → always low
    if not context or len(context) == 0:
        return "low"

    # Rule 2: Very short context (< 2 chunks) → medium max
    if len(context) < 2:
        return "medium" if confidence == "high" else confidence

    # Rule 3: Query is vague/short (< 5 words) → medium max
    if len(query.split()) < 5:
        return "medium" if confidence == "high" else confidence

    # Rule 4: Memory/follow-up queries with no DB history → medium
    if any(k in query.lower() for k in ["remember", "what did i", "previous", "last time"]):
        try:
            recent = db.get_recent_queries(n=1)
            if not recent:
                return "low"
        except Exception:
            return "low"

    # Rule 5: Healing queries — confidence from playbook, not LLM
    if any(k in query.lower() for k in ["crashloop", "oomkilled", "disk full", "service down"]):
        return confidence  # healer already sets this correctly

    return confidence


_WEAK_EVIDENCE_FALLBACK = "I do not have enough grounded evidence to answer this confidently."


def _should_use_weak_evidence_fallback(query, query_intent, confidence, context, response=None):
    """Prevent low-evidence DevOps answers from being filled in by model guesswork."""
    if query_intent in {"ava_self", "healing_incident"}:
        return False
    if query_intent in {"general", "general_qwen", "unknown"} and _should_direct_unknown_to_llm(query):
        return False
    if _has_unsupported_specific_terms(query, context):
        return True
    if confidence != "low":
        return False
    if response and is_weak_response(response):
        return True
    return True


def _build_weak_evidence_fallback(query, query_intent, confidence, context):
    next_steps = [
        "Share the exact component, error message, log line, or platform involved.",
        "Ask AVA to run a live diagnostic if this is about the current system.",
        "Use verified runtime facts instead of relying on weak retrieved chunks.",
    ]
    return (
        f"{_WEAK_EVIDENCE_FALLBACK}\n\n"
        "What I can do safely:\n"
        + "\n".join(f"- {step}" for step in next_steps)
    )


_COMMON_GROUNDING_TERMS = {
    "architecture", "application", "applications", "cluster", "clusters",
    "container", "containers", "deployment", "deployments", "docker",
    "certificate", "coredns", "crashloopbackoff", "describe", "explain", "failing", "failure", "failures",
    "health", "imagepullbackoff", "ingress", "investigate", "issue", "issues", "kubernetes", "liveness", "oomkilled",
    "artifact", "artifacts", "build", "cache", "cicd", "contention", "database", "destinationrule",
    "drift", "envoy", "eviction", "evictions", "istio", "latency", "locks", "mtls",
    "mitigation", "network", "networking", "namespace", "namespaces",
    "operator", "orchestration", "pattern", "patterns", "pending", "platform",
    "pipeline", "postgres", "postgresql", "practical", "readiness", "redis",
    "registry", "remediation", "replication", "request", "requests", "safely",
    "scan", "service", "services", "sidecar", "slowlog", "state", "system", "systems",
    "terraform", "timeouts", "tls", "traffic", "troubleshoot", "troubleshooting",
    "virtualservice", "workload", "workloads",
}


def _context_to_text(context):
    parts = []
    for item in context or []:
        if hasattr(item, "content"):
            parts.append(str(getattr(item, "content", "")))
        else:
            parts.append(str(item))
    return "\n".join(parts).lower()


def _specific_query_terms(query):
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{6,}", query.lower())
    return [
        token
        for token in tokens
        if token not in _CONFIDENCE_STOP_WORDS and token not in _COMMON_GROUNDING_TERMS
    ]


def _has_unsupported_specific_terms(query, context):
    specific_terms = _specific_query_terms(query)
    if not specific_terms:
        return False
    context_text = _context_to_text(context)
    if not context_text:
        return True
    return any(term not in context_text for term in specific_terms)


_CONFIDENCE_PREFIXES = {
    'medium': "Based on related documentation, ",
    'low': "I don't have a strong match for this, but based on general DevOps knowledge: ",
}

def _answer_ava_self_query(query, about=None):
    return _resolve_ava_self_response(query, about=about)


def _should_direct_unknown_to_llm(query, route=None):
    route = route or _route_query(query)
    if route.intent not in ("general_qwen", "unknown"):
        return False
    q = _normalize_user_query(query).lower().strip()
    if not q or _is_healing_query(q):
        return False
    blocked_markers = (
        "delete", "remove", "destroy", "drop ", "truncate", "wipe", "shutdown",
        "restart", "stop ", "terminate", "kill ", "run ", "execute", "apply ",
        "kubectl", "docker ", "systemctl", "sudo ", "chmod ", "chown ", "rm ",
        "del ", "format ", "curl ", "wget ", "ssh ", "scp ", "powershell ",
        "cmd /c", "bash ", "sh ", "write a command", "give me a command to run",
    )
    return not any(marker in q for marker in blocked_markers)


def _resolve_general_unknown_response(query, prior_messages=None, route=None):
    route = route or _route_query(query)
    if not _should_direct_unknown_to_llm(query, route=route):
        return None
    messages = [{
        "role": "system",
        "content": (
            "You are AVA, a helpful AI assistant. "
            "This is a general question outside AVA's local DevOps knowledge base. "
            "Answer it directly from your model knowledge in clear English. "
            "Do not mention missing retrieved context, internal tools, or AVA runtime details unless the user asked about them."
        ),
    }]
    if prior_messages:
        messages.extend(prior_messages)
    messages.append({"role": "user", "content": query})
    response = ollama.chat(
        model=LLM_MODEL,
        messages=messages,
        options={"num_ctx": 4096, "temperature": 0.2},
    )
    return {
        "response": _normalize_text(response["message"]["content"]),
        "confidence": "medium",
        "sources_used": 0,
    }


def generate_response(query, context, confidence=None, prior_messages=None):
    """Phase 3: Structured response with memory, CoT, Mermaid, fail-safe.
    Phase 5B: prior_messages injects up to 3 turns of conversation history.
    """
    try:
        memory_ctx = build_memory_context(query=query)
        query_intent = detect_query_intent(query)
        use_structured = is_technical_query(query)
        strict_grounding = _needs_strict_grounding(query_intent, query)

        system_parts = ["You are AVA, a senior DevOps AI assistant.\n\nRULES:\n1. Always respond in English only\n2. Use the provided context as your primary grounding source\n3. If the context is partial but clearly relevant, give the best direct answer you can and say what is inferred\n4. Do not lead with phrases like 'the context does not directly say' unless the context is genuinely irrelevant\n5. Prefer direct practical answers over meta commentary about the context\n6. Quote specific config values, commands, and fixes directly from context when available\n7. Be specific, practical, and concise"]

        if memory_ctx:
            system_parts.append(f"\nUSER CONTEXT (use this to tailor your answers):\n{memory_ctx}")

        if use_structured:
            system_parts.append("\nFor technical problems, always structure your answer as:\n\n**Root Cause:** [1-2 sentences]\n**Fix:** [exact commands or config]\n**Why this works:** [1-2 sentences]\n**Watch out for:** [edge cases or caveats]")

        if strict_grounding:
            system_parts.append("\nSTRICT GROUNDING MODE: ONLY use the provided context. Do not use general knowledge or fill gaps from model memory. If the grounded context is insufficient, say exactly: 'I don't have enough grounded context to answer that reliably.'")

        system_prompt = "\n".join(system_parts)

        if strict_grounding and not _grounding_confident_enough(query, context or [], confidence):
            return "I don't have enough grounded context to answer that reliably."

        if context:
            trimmed_context = [block[:900] for block in context[:6]]
            context_str = "\n\n---\n\n".join(trimmed_context)
            user_msg = (
                f"<context>\n{context_str}\n</context>\n\n"
                f"Question: {query}\n\n"
                "Answer using the context first, but if the context is only partially relevant, provide the most useful direct answer you can and briefly separate inference from explicit context."
            )
        else:
            user_msg = f"Answer this DevOps question from your knowledge:\n{query}"

        # Phase 5B: inject prior conversation turns before current message
        messages = [{"role": "system", "content": system_prompt}]
        if prior_messages:
            messages.extend(prior_messages)   # alternating user/assistant turns
        messages.append({"role": "user", "content": user_msg})

        response = ollama.chat(
            model=LLM_MODEL,
            messages=messages,
            options={"num_ctx": 8192, "temperature": 0.0 if strict_grounding else 0.2}
        )
        answer = response["message"]["content"]
        prefix = _CONFIDENCE_PREFIXES.get(confidence, "")
        return prefix + answer if prefix else answer

    except Exception as e:
        logger.error(f"LLM error: {e}")
        return "I could not generate a response right now. Please try again or use a more specific operational check."

# Routes
# ── Auth Endpoints ────────────────────────────────────────────────────────────

def _get_about_data() -> dict:
    """Return AVA system facts. Called by /about and ava_self context injection."""
    _collections = [
        "devops_policies_v2", "devops_blogs_v1",
        "devops_fixes_v1", "devops_patterns_v1",
    ]
    kb = {}
    for col in _collections:
        try:
            kb[col] = chroma_client.get_collection(col).count()
        except Exception:
            kb[col] = 0

    return {
        "version": "2.1.2",
        "phase": "Phase 5C Complete",
        "built_by": "Manoj, Delhi",
        "github": "linuxlearning38/agentic-safety-gate",
        "runtime": "WSL2 Ubuntu, RTX 5060 Ti 16GB, Ryzen 1600, 32GB RAM",
        "containers": {
            "ava-agent":       {"port": 5443, "proto": "HTTPS", "stack": "Flask/Gunicorn, 2 workers"},
            "agent_postgres":  {"port": 5432, "stack": "PostgreSQL 15"},
            "agent_redis":     {"port": 6379, "stack": "Redis 7"},
            "agent_opa":       {"port": 8181, "stack": "Open Policy Agent"},
            "agent_vault":     {"port": 8200, "stack": "HashiCorp Vault"},
        },
        "models": {
            "llm":       "qwen2.5:14b (Q4_K_M quantization)",
            "embedding": "nomic-embed-text",
            "vision":    "llava:13b",
            "ollama_host": "http://host.docker.internal:11434",
        },
        "knowledge_base": kb,
    }


@app.route('/about', methods=['GET'])
def about():
    """Public endpoint — no JWT required. Returns AVA system info."""
    return jsonify(_get_about_data())


def _check_dependencies() -> dict:
    """Check if critical dependencies are alive."""
    status = {"redis": False, "opa": False, "ollama": False}

    # Redis check — use raw socket (redis package may not be installed)
    try:
        import socket as _socket
        s = _socket.create_connection(("agent_redis", 6379), timeout=1)
        s.sendall(b"PING\r\n")
        reply = s.recv(16)
        s.close()
        status["redis"] = reply.startswith(b"+PONG")
    except Exception:
        pass

    # OPA check
    try:
        import requests as _requests
        resp = _requests.get("http://agent_opa:8181/health", timeout=1)
        status["opa"] = resp.status_code == 200
    except Exception:
        pass

    # Ollama check
    try:
        import requests as _requests
        resp = _requests.get("http://host.docker.internal:11434/api/tags", timeout=2)
        status["ollama"] = resp.status_code == 200
    except Exception:
        pass

    return status


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'dependencies': _check_dependencies()}), 200

@app.route('/auth/login', methods=['POST'])
@limiter.limit("10 per minute", key_func=get_remote_address)
def auth_login():
    """
    POST /auth/login
    Body: {"username": "admin", "password": "ava-admin-2026"}
    Returns: {"access_token": "...", "username": "...", "role": "...", "expires_in": 86400}
    """
    try:
        data     = request.json or {}
        username = data.get("username", "").strip()
        password = data.get("password", "")

        if not username or not password:
            return jsonify({"error": "username and password are required"}), 400

        user = verify_credentials(username, password)
        if not user:
            return jsonify({"error": "Invalid credentials"}), 401

        token = make_token(user["username"], user["role"])
        logger.info(f"[Auth] Token issued: user='{user['username']}' role='{user['role']}'")

        return jsonify({
            "access_token": token,
            "token_type":   "Bearer",
            "username":     user["username"],
            "role":         user["role"],
            "expires_in":   86400,   # 24h in seconds
        })

    except Exception as e:
        logger.error(f"[Auth] Login error: {e}")
        return jsonify({"error": "Login failed"}), 500


@app.route('/auth/me', methods=['GET'])
@jwt_required()
def auth_me():
    """
    GET /auth/me
    Returns current user info from JWT claims.
    Useful for frontend to verify token and get role.
    """
    identity = get_jwt_identity()
    claims   = get_jwt()
    return jsonify({
        "username": identity,
        "role":     claims.get("role", "unknown"),
    })


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

def split_multi_query(query):
    """Split a multi-part query into individual questions"""
    import re
    
    # Don't split if query is very short
    if len(query.split()) < 5:
        return [query]
    
    # Patterns that indicate multiple questions
    separators = [
        r',\s+',                  # Comma (most common)
        r'\.\s+(?=[A-Z])',        # Period followed by capital letter
        r'\?\s+',                 # Question mark
        r'\.\s+also\s+',          # "also"
        r'\.\s+and\s+',           # "and" 
        r'\s+too\.\s+',           # "too"
        r'\s+also\s+',            # "also" without period
    ]
    
    # Split by any of these patterns
    parts = [query]
    for separator in separators:
        new_parts = []
        for part in parts:
            split_result = re.split(separator, part, flags=re.IGNORECASE)
            new_parts.extend([p.strip() for p in split_result if p.strip()])
        parts = new_parts
    
    # Filter out very short parts (likely noise)
    questions = [p for p in parts if len(p.split()) >= 3]
    
    # If we got more than 5 questions, something went wrong - return original
    if len(questions) > 5:
        return [query]
    
    # If we only got 1 question back, return original
    if len(questions) <= 1:
        return [query]
    
    return questions

def detect_multiple_questions(query):
    """Detect and split a message containing multiple questions (max 3).

    Handles two patterns:
    - Numbered:  "1. How to deploy? 2. What is Docker? 3. ..."
    - ?-delimited: "How to deploy? What is Docker?"

    Returns a list of stripped question strings. Returns [query] for single questions.
    """
    import re

    # Pattern 1: numbered items — "1." / "1)" at word boundary
    numbered = re.split(r'\s*\d+[.)]\s+', query.strip())
    numbered = [q.strip() for q in numbered if q.strip()]
    if len(numbered) >= 2:
        return [q if q.endswith('?') else q for q in numbered[:3]]

    # Pattern 2: "?" followed by non-whitespace-only content
    parts = re.split(r'\?\s+', query.strip())
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        # Re-attach the "?" to each part except the last (which may already have it)
        questions = [p + '?' for p in parts[:-1]]
        last = parts[-1]
        questions.append(last if last.endswith('?') else last + '?')
        return questions[:3]

    if "," in query and looks_like_operational_request(query):
        split_parts = split_multi_query(query)
        if len(split_parts) >= 2:
            return split_parts[:3]

    return [query]


def is_greeting(query):
    """Check if query is a greeting"""
    greetings = ['hi', 'hello', 'hey', 'good morning', 'good afternoon', 'good evening', 'how are you', 'whats up', 'sup']
    query_lower = query.lower().strip()
    # Check if query is just a greeting (possibly with punctuation)
    query_clean = query_lower.replace('?', '').replace('!', '').replace(',', '').strip()
    return query_clean in greetings or any(query_clean == g for g in greetings)

def is_meta_question(query):
    """Check if query is about AVA itself"""
    meta_patterns = [
        'what can you do', 'what do you do', 'what are you', 'who are you',
        'what is your', 'introduce yourself', 'tell me about yourself',
        'what are your capabilities', 'what can you help', 'how can you help',
        'what is ava', 'tell me about ava', 'what\'s your purpose',
        # Model-related questions
        'which model', 'what model', 'which llm', 'what llm',
        'are you using', 'do you use'
    ]
    query_lower = query.lower()
    return any(pattern in query_lower for pattern in meta_patterns)

def get_ava_introduction():
    """Return AVA's self-introduction"""
    return """I'm **AVA** (Automated Virtual Assistant) - a specialized local DevOps AI agent running entirely on your machine.

### What Makes Me Different

**🔒 Private & Secure**
- Everything runs locally (no cloud, no data leaks)
- OPA policy enforcement for command safety
- Whitelisted command execution only
- No telemetry or external API calls

**📚 Specialized Knowledge Base**
- **3,885 curated chunks** from 5 GitHub repositories:
  - DevOps Exercises (real-world Q&A scenarios)
  - AWS DevOps Zero to Hero
  - Jenkins Zero to Hero  
  - Docker Zero to Hero
  - Terraform Zero to Hero
  - mrcloudbook.com best practices
- Powered by Qwen 2.5 14B (local LLM)
- ChromaDB for semantic search

### What I Can Do

**💡 Answer DevOps Questions**
- AWS architecture, S3, RDS, VPC, IAM
- Kubernetes deployments and strategies
- Docker image optimization
- Terraform infrastructure-as-code
- Jenkins CI/CD pipelines

**📄 Analyze Your Files**
- Terraform (.tf, .hcl)
- Docker (Dockerfile)
- Kubernetes (.yaml, .yml)
- Shell scripts (.sh)
- Python (.py)
- JSON configs

**⚡ Execute Safe Commands**
- Run whitelisted shell commands
- Commands: date, whoami, pwd, ls, cat, grep, df, free, ps, top, uptime, etc.
- OPA blocks dangerous operations

**🔍 Provide Working Examples**
- AWS CLI commands
- Infrastructure code
- Configuration files
- Best practices

### What I'm NOT

- ❌ Not a general-purpose chatbot
- ❌ Not connected to the internet
- ❌ Not sending your data anywhere
- ❌ Not replacing human expertise (verify critical decisions!)

### Try Me With

- "How to secure S3 buckets in production?"
- "Analyze this Terraform file" (upload a .tf file)
- "Design a highly available RDS setup"
- "Run pwd" (safe command execution)

I'm here to help with your DevOps journey - ask me anything!"""

@app.route('/ask', methods=['POST'])
@jwt_required()
@limiter.limit("20 per minute")
def ask():
    start_time = time.time()
    try:
        data = request.json or {}
        raw_query = (data.get('query', '') or '').strip()
        normalized_query = _normalize_user_query(raw_query)
        preserve_raw_query = any((
            extract_explicit_command_request(raw_query),
            extract_operational_tool_request(raw_query),
            extract_operational_clarification(raw_query),
            _is_compound_dangerous_request(raw_query),
            _is_single_destructive_request(raw_query),
        ))
        query = raw_query if preserve_raw_query else normalized_query

        if not query:
            return jsonify({'error': 'No query provided'}), 400

        logger.info(f"Query: {query}")
        controlled_route = _route_query(query)

        # Dependency gate only for routes that genuinely require the general LLM path.
        deps = _check_dependencies()
        if controlled_route.intent == "general_qwen" and not deps["ollama"]:
            logger.warning("[/ask] Ollama unavailable for general_qwen — returning 503")
            return jsonify({
                "error":        "ollama_unavailable",
                "confidence":   "low",
                "response":     "LLM service is unavailable. Cannot process this general query right now.",
                "dependencies": deps,
            }), 503

        if controlled_route.intent == "memory_store":
            resolved = _resolve_memory_store_response(query)
            response = resolved["response"]
            elapsed = time.time() - start_time
            _record_query(query, response, "memory_store", elapsed, confidence=resolved["confidence"])
            return jsonify(_chat_payload(
                response,
                response_type="memory",
                confidence=resolved["confidence"],
                time_taken=f"{elapsed:.2f}s",
            ))

        if controlled_route.intent == "memory_recall":
            resolved = _resolve_memory_recall_response(query)
            response = resolved["response"]
            confidence = resolved["confidence"]
            elapsed = time.time() - start_time
            _record_query(query, response, "memory_recall", elapsed, confidence=confidence)
            return jsonify(_chat_payload(
                response,
                response_type="memory",
                confidence=confidence,
                time_taken=f"{elapsed:.2f}s",
            ))

        if _is_compound_dangerous_request(query):
            elapsed = time.time() - start_time
            blocked_result = _blocked_action_result(
                query,
                "Multiple destructive actions in one request are blocked. Submit one action at a time for review.",
                "compound_destructive_request",
            )
            save_history({
                'timestamp': datetime.now().isoformat(),
                'query': query,
                'type': 'command',
                'blocked': True,
                'time_taken': f"{elapsed:.2f}s"
            })
            return jsonify(_chat_payload(
                _command_response_text(blocked_result),
                response_type='command',
                time_taken=f"{elapsed:.2f}s",
                result=_build_command_response(blocked_result),
            ))

        # Multi-question detection — split and answer each separately before
        # generic knowledge routing so operational prompts do not collapse.
        questions = detect_multiple_questions(query)
        if len(questions) > 1:
            logger.info(f"[*] Multi-question detected: {len(questions)} questions")
            combined_results = []
            total_sources = 0

            for idx, q in enumerate(questions, 1):
                logger.info(f"[{idx}/{len(questions)}] Processing: {q}")
                q_route = _route_query(q)
                q_resolved = _resolve_controlled_query(q, controlled_route=q_route)
                if not q_resolved:
                    q_resolved = _resolve_grounded_knowledge_query(q)

                total_sources += q_resolved.get("sources_used", 0)
                if q_resolved["type"] == "command":
                    combined_results.append({
                        "number": idx,
                        "question": q,
                        "type": "command",
                        "result": q_resolved["result"],
                    })
                else:
                    combined_results.append({
                        "number": idx,
                        "question": q,
                        "type": q_resolved["type"],
                        "response": q_resolved["response"],
                    })

            elapsed = time.time() - start_time
            save_history({
                'timestamp': datetime.now().isoformat(),
                'query': query,
                'type': 'multi',
                'parts': len(questions),
                'time_taken': f"{elapsed:.2f}s"
            })
            return jsonify(_chat_payload(
                "",
                response_type='multi',
                sources_used=total_sources,
                time_taken=f"{elapsed:.2f}s",
                results=combined_results,
            ))

        prior_messages = _get_recent_prior_messages(n=2) if _should_direct_unknown_to_llm(query, route=controlled_route) and not looks_like_operational_request(query) else None
        controlled_resolved = _resolve_controlled_query(query, controlled_route=controlled_route, prior_messages=prior_messages)
        if controlled_resolved:
            elapsed = time.time() - start_time
            return jsonify(_finalize_resolved_payload(query, controlled_resolved, elapsed))
        
        # ── Phase 4: Command Graph — deterministic diagnostics ──────────────
        # Skip command graph for knowledge/troubleshooting questions that don't
        # explicitly request command execution. Graph is for live diagnostics only.
        _graph_explicit = any(k in query.lower() for k in [
            "run kubectl", "execute", "apply the fix", "apply this", "run this",
            "run the", "kubectl apply", "kubectl exec", "diagnose now", "check now",
        ])
        graph_name = match_graph(query) if _graph_explicit or detect_query_intent(query) not in ("troubleshooting", "definition", "ava_self", "healing_incident") else None
        if graph_name:
            logger.info(f"[*] Command Graph matched: {graph_name}")
            _graph_t0    = time.time()
            graph_result = execute_graph(graph_name, query)
            _graph_dur   = time.time() - _graph_t0

            # Report graph execution
            try:
                report_graph_execution(
                    graph_name   = graph_name,
                    graph_result = graph_result,
                    triggered_by = get_jwt_identity(),
                    ip_address   = request.remote_addr,
                    query        = query,
                    duration     = _graph_dur,
                )
            except Exception as _re:
                logger.warning(f"[Reporter] Graph report failed: {_re}")

            # If a medium-risk step needs approval, pause and tell the user
            if graph_result.paused_at:
                elapsed = time.time() - start_time
                return jsonify(_finalize_resolved_payload(query, {
                    "type": "knowledge",
                    "intent": "command_graph",
                    "response": (
                        f"⚠️ **Approval Required**\n\n"
                        f"I ran the `{graph_name}` diagnostic and reached a step that "
                        f"needs your approval before continuing:\n\n"
                        f"**Tool:** `{graph_result.paused_at}`\n"
                        f"**Approval ID:** `{graph_result.approval_id}`\n\n"
                        f"Run this to approve:\n"
                        f"```bash\npython3 -m control.security_review\n```\n\n"
                        f"Steps completed so far:\n{graph_result.summary_for_ui()}"
                    ),
                    "sources_used": 0,
                    "graph_used": graph_name,
                }, elapsed))

            # Build context from live tool outputs → send to LLM for analysis
            context_blocks = graph_result.to_context_blocks()
            framing = (
                f"The following is LIVE diagnostic output from running the "
                f"'{graph_name}' diagnostic on the user's system. "
                f"Analyse the output and give a specific diagnosis and fix.\n"
            )
            context_blocks.insert(0, framing)

            response = generate_response(query, context_blocks)

            elapsed = time.time() - start_time
            return jsonify(_finalize_resolved_payload(query, {
                "type": "knowledge",
                "intent": "command_graph",
                "response": response,
                "sources_used": len(context_blocks),
                "graph_used": graph_name,
                "steps_run": [
                    {'tool': s['tool'], 'status': s['status']}
                    for s in graph_result.steps_run
                ],
            }, elapsed))
        # ── End Command Graph ───────────────────────────────────────────────

        # ── Phase 4: ReAct Loop — for complex/unknown problems ─────────────
        # Runs when no command graph matched AND query looks like a real problem
        # not just a knowledge question
        react_signals = [
            "not working", "broken", "failing", "failed", "down", "crash",
            "error", "issue", "problem", "stuck", "slow", "high latency",
            "can't connect", "cannot connect", "unreachable", "timeout",
            "oom", "killed", "evicted", "pending", "unknown", "investigate",
            "diagnose", "debug", "troubleshoot", "why is", "what's wrong",
        ]
        is_problem_query = any(s in query.lower() for s in react_signals)

        # Troubleshooting knowledge questions → KNOWLEDGE branch, not ReAct.
        # Only use ReAct when user explicitly asks to run/execute something.
        _explicit_execution = any(k in query.lower() for k in [
            "run kubectl", "execute", "apply the fix", "apply this", "run this",
            "run the", "kubectl apply", "kubectl exec",
        ])
        _query_intent_here = detect_query_intent(query)
        if _query_intent_here == "troubleshooting" and not _explicit_execution:
            is_problem_query = False

        if is_problem_query and not any(k in query.lower() for k in [
            "how to", "how do", "what is", "explain", "best practice",
            "difference between", "compare", "show me", "create a"
        ]):
            logger.info("[*] ReAct loop triggered for problem query")
            # Seed with RAG context so LLM has background knowledge
            rag_context = query_knowledge_base(query, n_results=3)
            react_result = react_loop.run(query, initial_context=rag_context)

            logger.info(f"[ReAct] {react_result.summary_for_log()}")

            elapsed = time.time() - start_time
            return jsonify(_finalize_resolved_payload(query, {
                "type": "knowledge",
                "intent": "react",
                "response": react_result.final_answer or "I was unable to complete the diagnostic. Please check kubectl is available in this environment.",
                "sources_used": react_result.iterations,
                "react_trace": [
                    {
                        'iteration':    s.iteration,
                        'thought':      s.thought[:200],
                        'action':       s.action,
                        'observation':  (s.observation or '')[:300],
                        'final_answer': bool(s.final_answer),
                    }
                    for s in react_result.steps
                ],
            }, elapsed))
        # ── End ReAct Loop ──────────────────────────────────────────────────

        logger.info("[*] Searching knowledge base...")
        prior_messages = _get_recent_prior_messages(n=3)
        if prior_messages:
            logger.info(f"[MultiTurn] Injecting {len(prior_messages) // 2} prior turns")
        grounded_resolved = _resolve_grounded_knowledge_query(query, prior_messages=prior_messages)
        response = grounded_resolved["response"]
        confidence = grounded_resolved["confidence"]
        context = grounded_resolved["context"]
        query_intent = grounded_resolved["query_intent"]
        logger.info(f"[*] Found {len(context)} relevant chunks")
        logger.info(f"[*] Context confidence: {confidence}")
        logger.info(f"[*] Thinking with {LLM_MODEL}...")

        # Phase 4.5: Update live stats counters
        STATS['query_count'] += 1
        # token count: word count approximation (more accurate than char/4)
        response_tokens = len(response.split())
        STATS['total_tokens'] += response_tokens
        STATS['avg_tokens_per_query'] = STATS['total_tokens'] // STATS['query_count']

        elapsed = time.time() - start_time

        return jsonify(_finalize_resolved_payload(query, {
            "type": "knowledge",
            "intent": "knowledge",
            "response": response,
            "sources_used": len(context),
            "confidence": confidence,
        }, elapsed))
        
    except Exception as e:
        logger.error(f"Error in ask endpoint: {e}")
        return jsonify(_chat_payload(
            f"Failed to process query: {e}",
            response_type='error',
            ok=False
        )), 500

def _extract_webhook_message(payload: dict) -> str:
    """Normalise webhook payloads from Alertmanager, Datadog, PagerDuty, or generic."""
    # Alertmanager
    alerts = payload.get("alerts")
    if alerts and isinstance(alerts, list):
        firing = [a for a in alerts if a.get("status") == "firing"]
        if firing:
            return firing[0].get("annotations", {}).get("summary", "") or \
                   firing[0].get("annotations", {}).get("description", "")
        if alerts:
            return alerts[0].get("annotations", {}).get("summary", "")

    # Datadog
    if "title" in payload and "text" in payload:
        return f"{payload['title']}: {payload['text']}"

    # PagerDuty
    messages = payload.get("messages")
    if messages and isinstance(messages, list):
        try:
            return messages[0]["event"]["data"]["title"]
        except (KeyError, IndexError):
            pass

    # Generic
    return payload.get("message", payload.get("summary", payload.get("title", "")))


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Phase 5B — Webhook trigger endpoint.
    Accepts alerts from Alertmanager, Datadog, PagerDuty, or generic senders.
    Auth: X-Webhook-Secret header (no JWT required).
    """
    # Auth
    if not WEBHOOK_SECRET:
        logger.warning("[Webhook] Rejected request because WEBHOOK_SECRET is not configured")
        return jsonify({"status": "error", "message": "Webhook endpoint is disabled"}), 503

    provided_secret = request.headers.get("X-Webhook-Secret", "")
    if provided_secret != WEBHOOK_SECRET:
        logger.warning(f"[Webhook] Invalid secret from {request.remote_addr}")
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        payload = request.get_json(force=True, silent=True) or {}
        alert_msg = _extract_webhook_message(payload)
        if not alert_msg:
            return jsonify({"status": "error", "message": "Could not extract alert message from payload"}), 400

        logger.info(f"[Webhook] Alert received: {alert_msg[:120]}")

        # Phase 5C: classify + heal via SelfHealer
        issue        = healer.detect_issue(source="webhook", message=alert_msg)
        healing      = healer.heal(issue, dry_run=False)
        heal_action  = healing.get("action_taken", "none")
        logger.info(f"[Webhook] SelfHealer → {heal_action} | issue={issue['issue_type']}")

        # Also run through full AVA knowledge pipeline for a human-readable response
        query_intent = detect_query_intent(alert_msg)
        context      = query_knowledge_base(alert_msg, query_intent=query_intent)
        confidence   = score_context_confidence(context, alert_msg)
        response     = generate_response(alert_msg, context, confidence=confidence)

        # Persist to query history
        try:
            db.save_query(
                query=alert_msg,
                response=response,
                confidence=confidence,
                intent=query_intent,
                sources_used=len(context),
            )
        except Exception as _dbe:
            logger.warning(f"[Webhook] save_query failed: {_dbe}")

        return jsonify({
            "status":        "ok",
            "message":       response[:600],
            "confidence":    confidence,
            "action_taken":  heal_action,
            "healing": {
                "issue_type":   issue["issue_type"],
                "severity":     issue["severity"],
                "heal_confidence": issue["confidence"],
                "command_used": healing.get("command_used"),
                "result":       healing.get("result"),
                "risk_level":   healing.get("risk_level"),
            },
        })

    except Exception as e:
        logger.error(f"[Webhook] Error: {e}")
        return jsonify({"status": "error", "message": "Webhook processing failed"}), 500


@app.route('/heal', methods=['POST'])
@require_admin
def manual_heal():
    """
    Phase 5C — Manually trigger a healing action.
    Body: {"issue_type": "disk_full", "target": "worker-1", "dry_run": true}
    """
    try:
        data       = request.json or {}
        issue_type = data.get("issue_type", "").strip()
        target     = data.get("target", "unknown")
        dry_run    = bool(data.get("dry_run", True))

        if not issue_type:
            return jsonify({"error": "issue_type is required"}), 400
        from control.self_healer import HEALING_PLAYBOOK
        if issue_type not in HEALING_PLAYBOOK:
            return jsonify({
                "error": f"Unknown issue_type '{issue_type}'",
                "valid": list(HEALING_PLAYBOOK.keys()),
            }), 400

        issue = {
            "issue_type": issue_type,
            "severity":   "MEDIUM",
            "confidence": 0.90,   # manual trigger — assume high confidence
            "source":     f"manual:{get_jwt_identity()}",
            "entities": {
                "name":         target,
                "namespace":    data.get("namespace", "default"),
                "pod_name":     target,
                "node_name":    target,
                "service_name": target,
                "new_limit":    data.get("new_limit", "512Mi"),
                "old_limit":    data.get("old_limit", "256Mi"),
                "new_replicas": str(data.get("new_replicas", 3)),
                "old_replicas": str(data.get("old_replicas", 1)),
            },
        }

        result = healer.heal(issue, dry_run=dry_run)
        return jsonify({"status": "ok", "dry_run": dry_run, **result})

    except Exception as e:
        logger.error(f"[/heal] Error: {e}")
        return _api_error("Healing request failed")


@app.route('/healing/history', methods=['GET'])
@jwt_required()
def healing_history():
    """Phase 5C — Returns last 20 self-healing audit entries."""
    try:
        history = healer.get_healing_history(20)
        return jsonify({"history": history, "total": len(history)})
    except Exception as e:
        logger.error(f"[/healing/history] Error: {e}")
        return _api_error("Healing history is unavailable")


@app.route('/upload', methods=['POST'])
@require_admin
def upload_file():
    start_time = time.time()
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        file_bytes = file.read()
        filename = file.filename
        file_ext = filename.split('.')[-1].lower() if '.' in filename else 'unknown'

        # Phase 3: Image analysis with llava
        if file_ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']:
            import base64
            image_data = base64.b64encode(file_bytes).decode('utf-8')
            logger.info(f"Image detected: {filename} — using llava:13b")
            vision_response = ollama.chat(
                model="llava:13b",
                messages=[{
                    "role": "user",
                    "content": (
                        "Analyze this infrastructure/architecture diagram carefully.\n\n"
                        "STEP 1 - List ALL visible text labels, component names, and technology names "
                        "you can read in the image. Be exhaustive.\n\n"
                        "STEP 2 - For each component you identified, explain:\n"
                        "- What it does\n"
                        "- Where it sits in the architecture\n"
                        "- How it connects to other components\n\n"
                        "STEP 3 - Explain the complete request flow from left to right (or top to bottom).\n\n"
                        "STEP 4 - Explain the data flow if present.\n\n"
                        "IMPORTANT RULES:\n"
                        "- ONLY mention components you can actually see labeled in the image\n"
                        "- Do NOT add AWS/Kubernetes/Lambda unless you can see those exact labels\n"
                        "- If you see 'Zuul' label it as Zuul API Gateway\n"
                        "- If you see 'Kafka' label it as Kafka Message Queue\n"
                        "- Be specific, not generic"
                    ),
                    "images": [image_data]
                }]
            )
            vision_analysis = vision_response['message']['content']
            # Combine structural entities (from filename/text) with known-tech scan
            diagram_entities = _diagram_entities_from_text(filename, vision_analysis)
            tech_entities    = _extract_diagram_entities(vision_analysis)
            # Merge without duplicates, tech_entities first (higher signal)
            combined_keys = {e.lower() for e in diagram_entities}
            for te in tech_entities:
                if te not in combined_keys:
                    diagram_entities.append(te)
                    combined_keys.add(te)
            diagram_query = (
                f"Analyze this infrastructure diagram for {filename}. "
                f"Focus on these detected components: {', '.join(diagram_entities) if diagram_entities else 'unknown components'}."
            )
            diagram_context = [
                _build_diagram_grounding_block(diagram_query, [vision_analysis], diagram_entities)
            ]
            kb_context = query_knowledge_base(diagram_query, query_intent="architecture")
            if kb_context:
                diagram_context.extend(kb_context[:5])
            analysis = generate_response(
                diagram_query,
                diagram_context,
                confidence="high" if diagram_entities else "medium",
            )
            elapsed = time.time() - start_time
            save_history({
                'timestamp': datetime.now().isoformat(),
                'query': f"Image analysis: {filename}",
                'type': 'image_analysis',
                'filename': filename,
                'time_taken': f"{elapsed:.2f}s"
            })
            return jsonify({
                'type': 'file_analysis',
                'filename': filename,
                'analysis': analysis,
                'time_taken': f"{elapsed:.2f}s"
            })

        content = file_bytes.decode('utf-8', errors='ignore')
        
        logger.info(f"Analyzing file: {filename}")
        
        file_type = filename.split('.')[-1] if '.' in filename else 'unknown'
        query = f"""Analyze this {file_type} file ({filename}) and provide:
1. What it does
2. Any issues or improvements
3. Best practices recommendations

File content:
{content[:3000]}"""
        
        context = query_knowledge_base(f"best practices for {file_type} files")
        analysis = generate_response(query, context)
        elapsed = time.time() - start_time
        
        save_history({
            'timestamp': datetime.now().isoformat(),
            'query': f"File analysis: {filename}",
            'type': 'file_analysis',
            'filename': filename,
            'time_taken': f"{elapsed:.2f}s"
        })
        
        return jsonify({
            'type': 'file_analysis',
            'filename': filename,
            'analysis': analysis,
            'time_taken': f"{elapsed:.2f}s"
        })
        
    except Exception as e:
        logger.error(f"Error in upload endpoint: {e}")
        return _api_error("Failed to analyze file")

@app.route('/history', methods=['GET'])
@jwt_required()
def get_history():
    try:
        history = load_history()
        return jsonify({'history': history[-50:], 'total': len(history)})
    except Exception as e:
        logger.error(f"Error loading history endpoint: {e}")
        return _api_error("History is unavailable")

@app.route('/stats', methods=['GET'])
@jwt_required()
def get_stats():
    return jsonify(STATS)

@app.route('/execute_approved', methods=['POST'])
@require_admin
@limiter.limit("10 per minute")
def execute_approved_route():
    """
    Execute a command that was previously queued for approval.
    Called from the UI approval panel.

    Body: {"approval_id": "abc123"}
    """
    try:
        data        = request.json
        approval_id = data.get('approval_id', '').strip()

        if not approval_id:
            return jsonify({'error': 'approval_id is required'}), 400

        logger.info(f"[Approval] Executing approved command: {approval_id}")
        _t0    = time.time()
        result = execute_approved_command(approval_id)
        _dur   = time.time() - _t0

        report_approved_execution(
            approval_id  = approval_id,
            result       = result,
            triggered_by = get_jwt_identity(),
            ip_address   = request.remote_addr,
            duration     = _dur,
        )

        if result.get('status') == 'success':
            return jsonify({
                'status':  'executed',
                'command': result.get('command_repr', ''),
                'output':  result.get('output', ''),
                'mode':    result.get('mode', ''),
                'risk':    result.get('risk', ''),
            })
        else:
            return jsonify({
                'status': 'error',
                'error':  result.get('error') or result.get('reason') or 'Unknown error',
            }), 400

    except Exception as e:
        logger.error(f"Error in execute_approved: {e}")
        return _api_error("Approved command execution failed")


@app.route('/tools', methods=['GET'])
@jwt_required()
def list_tools_route():
    """
    List all registered tools with their risk levels and descriptions.
    Used by the UI to display available tools.
    """
    try:
        tools = tool_registry.list_tools()
        by_risk = {
            'low':    [t for t in tools if t['risk_level'] == 'low'],
            'medium': [t for t in tools if t['risk_level'] == 'medium'],
            'high':   [t for t in tools if t['risk_level'] == 'high'],
        }
        return jsonify({
            'total': len(tools),
            'by_risk': by_risk,
            'tools': tools,
        })
    except Exception as e:
        logger.error(f"Error listing tools: {e}")
        return _api_error("Tool list is unavailable")


@app.route('/tools/<tool_name>/run', methods=['POST'])
@require_admin
@limiter.limit("10 per minute")
def run_tool_route(tool_name):
    """
    Directly run a LOW risk tool from the UI.
    Medium/high risk tools go through the approval workflow.

    Body: {"args": {"namespace": "default", ...}}
    """
    try:
        data      = request.json or {}
        tool_args = data.get('args', {})

        tool = tool_registry.get_tool(tool_name)
        if not tool:
            return jsonify({'error': f"Tool '{tool_name}' not found"}), 404

        logger.info(f"[Tool] Direct run: {tool_name}({tool_args})")
        _t0    = time.time()
        result = execute_tool_safe(tool_name, tool_args, query=f"tool_route:{tool_name}", source="tools_route")
        _dur   = time.time() - _t0

        report_tool_execution(
            tool_name    = tool_name,
            tool_args    = tool_args,
            result       = result,
            triggered_by = get_jwt_identity(),
            ip_address   = request.remote_addr,
            duration     = _dur,
        )

        return jsonify({
            'tool':    tool_name,
            'status':  result.get('status'),
            'output':  result.get('output', ''),
            'error':   result.get('error', ''),
            'approval_id': result.get('approval_id'),
            'risk': result.get('risk'),
            'reason': result.get('reason', ''),
        })

    except Exception as e:
        logger.error(f"Error running tool {tool_name}: {e}")
        return _api_error("Tool execution failed")


@app.route('/react/run', methods=['POST'])
@require_admin
@limiter.limit("5 per minute")
def react_run_route():
    """
    Directly trigger the ReAct loop for a query.
    Returns full trace including all iterations.

    Body: {"query": "my nginx pod is slow"}
    """
    try:
        data  = request.json
        query = data.get('query', '').strip()

        if not query:
            return jsonify({'error': 'query is required'}), 400

        logger.info(f"[ReAct Direct] Query: {query}")
        _t0          = time.time()
        react_result = react_loop.run(query)
        _dur         = time.time() - _t0

        report_react_execution(
            react_result = react_result,
            triggered_by = get_jwt_identity(),
            ip_address   = request.remote_addr,
            query        = query,
            duration     = _dur,
        )

        return jsonify({
            'query':        query,
            'final_answer': react_result.final_answer,
            'iterations':   react_result.iterations,
            'stopped':      react_result.stopped_reason,
            'success':      react_result.success,
            'trace': [
                {
                    'iteration':    s.iteration,
                    'thought':      s.thought,
                    'action':       s.action,
                    'action_input': s.action_input,
                    'observation':  s.observation,
                    'final_answer': s.final_answer,
                }
                for s in react_result.steps
            ],
        })

    except Exception as e:
        logger.error(f"Error in react_run: {e}")
        return _api_error("ReAct execution failed")


# ── Day 7: Incident Report Endpoints ─────────────────────────────────────────

@app.route('/reports', methods=['GET'])
@require_admin
def list_reports():
    """
    GET /reports?limit=20
    List recent incident reports (newest first). Admin only.
    """
    try:
        limit   = min(int(request.args.get('limit', 20)), 100)
        reports = get_recent_reports(limit)
        return jsonify({
            'total':   len(reports),
            'reports': reports,
        })
    except Exception as e:
        logger.error(f"[Reports] list error: {e}")
        return _api_error("Reports are unavailable")


@app.route('/reports/stats', methods=['GET'])
@require_admin
def reports_stats():
    """
    GET /reports/stats
    Summary statistics across all reports. Admin only.
    """
    try:
        return jsonify(get_reports_stats())
    except Exception as e:
        logger.error(f"[Reports] stats error: {e}")
        return _api_error("Report statistics are unavailable")


@app.route('/reports/<report_id>', methods=['GET'])
@require_admin
def get_report(report_id):
    """
    GET /reports/<report_id>
    Fetch a full report by ID. Admin only.
    """
    try:
        report = get_report_by_id(report_id)
        if not report:
            return jsonify({'error': f'Report {report_id} not found'}), 404
        return jsonify(report)
    except Exception as e:
        logger.error(f"[Reports] get error: {e}")
        return _api_error("Report is unavailable")


@app.route('/rate-limit/status', methods=['GET'])
@jwt_required()
def rate_limit_status():
    """
    GET /rate-limit/status
    Shows current rate limit config for the authenticated user.
    Useful for debugging and monitoring.
    """
    identity = get_jwt_identity()
    claims   = get_jwt()
    role     = claims.get("role", "unknown")

    limits = {
        "user":    identity,
        "role":    role,
        "limits":  {
            "default":           "30 per minute",
            "/ask":              "20 per minute",
            "/tools/<n>/run":    "10 per minute",
            "/execute_approved": "10 per minute",
            "/react/run":        "5 per minute",
            "/auth/login":       "10 per minute (per IP, unauthenticated)",
        },
        "storage": RATE_LIMIT_STORAGE_URI,
        "headers": "X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset",
        "note":    "Rate limits are per-user for authenticated endpoints, per-IP for login.",
    }
    return jsonify(limits)


@app.route('/security/posture', methods=['GET'])
@require_admin
def security_posture_route():
    """
    GET /security/posture
    Admin-only runtime posture summary for the UI. This reports active
    zero-trust controls instead of relying on static documentation.
    """
    try:
        docker_host = os.getenv("DOCKER_HOST", "")
        webhook_secret_configured = bool(os.getenv("WEBHOOK_SECRET", "").strip())
        monitor_enabled = os.getenv("AVA_MONITOR_ENABLED", "false").lower() == "true"
        warmup_enabled = os.getenv("LLM_WARMUP_ENABLED", "false").lower() == "true"

        controls = [
            {
                "name": "Security telemetry requires admin",
                "status": "pass",
                "detail": "/security/stats and /security/audit are protected by admin JWT.",
            },
            {
                "name": "Webhook has no default secret",
                "status": "pass" if webhook_secret_configured else "warn",
                "detail": (
                    "Explicit WEBHOOK_SECRET is configured."
                    if webhook_secret_configured
                    else "Webhook is disabled until WEBHOOK_SECRET is explicitly configured."
                ),
            },
            {
                "name": "Rate limiting uses shared storage",
                "status": "pass" if RATE_LIMIT_STORAGE_URI.startswith("redis://") else "warn",
                "detail": RATE_LIMIT_STORAGE_URI,
            },
            {
                "name": "Docker access is proxied",
                "status": "pass" if docker_host.startswith("http://docker-socket-proxy") else "warn",
                "detail": docker_host or "DOCKER_HOST not set; Docker runtime will fall back to local socket rules.",
            },
            {
                "name": "Autonomous monitor is opt-in",
                "status": "pass" if not monitor_enabled else "warn",
                "detail": "disabled" if not monitor_enabled else "enabled",
            },
            {
                "name": "LLM warmup is opt-in",
                "status": "pass" if not warmup_enabled else "warn",
                "detail": "disabled" if not warmup_enabled else "enabled",
            },
        ]

        posture = {
            "mode": "zero-trust-aligned local hardening",
            "perfect_zero_trust": False,
            "summary": "AVA is hardened for local/personal penetration testing, but enterprise zero-trust still needs signed agents, mTLS, policy-backed fleet actions, and tamper-resistant audit storage.",
            "rate_limit_storage": RATE_LIMIT_STORAGE_URI,
            "docker_access": "proxy" if docker_host else "local_socket_fallback",
            "webhook_enabled": webhook_secret_configured,
            "autonomous_monitor_enabled": monitor_enabled,
            "llm_warmup_enabled": warmup_enabled,
            "runtime_paths": {
                "db": os.getenv("DB_PATH", ""),
                "history": os.getenv("HISTORY_FILE", ""),
                "data_dir": os.getenv("AVA_DATA_DIR", ""),
                "trivy_cache": os.getenv("TRIVY_CACHE_DIR", ""),
            },
            "controls": controls,
            "remaining_gaps": [
                "No mTLS or signed command protocol for future remote agents yet.",
                "Audit log integrity is not tamper-resistant yet.",
                "Container root filesystem is not read-only yet; this was attempted but needs a dedicated compatibility pass.",
                "OPA is present, but not every action is policy-decided through OPA yet.",
            ],
            "recommended_next_ui_actions": [
                "verify my system",
                "check docker",
                "look for suspicious activity",
                "scan my system for vulnerabilities",
            ],
        }
        return jsonify(posture)
    except Exception as e:
        logger.error(f"Error getting security posture: {e}")
        return _api_error("Security posture is unavailable")


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(429)
def rate_limit_exceeded_handler(e):
    """Return JSON on 429 — not Flask default HTML."""
    retry_after = 60
    try:
        retry_after = int(e.retry_after)
    except Exception:
        pass
    logger.warning(f"[RateLimit] 429: path={request.path} key={_rate_limit_key()}")
    return jsonify({
        "error":               "Rate limit exceeded. Slow down.",
        "code":                "rate_limit_exceeded",
        "retry_after_seconds": retry_after,
    }), 429


@app.route('/security/stats', methods=['GET'])
@require_admin
def get_security_stats_route():
    """Get security statistics for dashboard"""
    try:
        from control.approval import get_pending
        from datetime import timedelta
        
        # Load audit log
        audit_log_path = os.getenv("SECURITY_AUDIT_LOG", "/home/manoj/ava-data/security_audit.json")
        if os.path.exists(audit_log_path):
            with open(audit_log_path, 'r') as f:
                audit_log = json.load(f)
        else:
            audit_log = []
        
        # Get stats for last 24 hours
        cutoff = datetime.now() - timedelta(hours=24)
        recent = [
            entry for entry in audit_log
            if datetime.fromisoformat(entry['timestamp']) > cutoff
        ]
        
        stats = {
            'total_commands': len(recent),
            'executed': len([e for e in recent if e['event_type'] == 'executed']),
            'blocked': len([e for e in recent if e['event_type'] == 'blocked']),
            'pending': len(get_pending()),
            'high_risk': len([e for e in recent if e.get('risk_analysis', {}).get('risk') in ['high', 'critical']]),
            'threats_detected': sum(len(e.get('threats', [])) for e in recent)
        }
        
        return jsonify(stats)
        
    except Exception as e:
        logger.error(f"Error getting security stats: {e}")
        return _api_error("Security statistics are unavailable")

@app.route('/security/audit', methods=['GET'])
@require_admin
def get_audit_log_route():
    """Get audit log entries"""
    try:
        count = int(request.args.get('count', 10))
        
        audit_log_path = os.getenv("SECURITY_AUDIT_LOG", "/home/manoj/ava-data/security_audit.json")
        if os.path.exists(audit_log_path):
            with open(audit_log_path, 'r') as f:
                audit_log = json.load(f)
        else:
            audit_log = []
        
        return jsonify({
            'total': len(audit_log),
            'entries': audit_log[-count:]
        })
        
    except Exception as e:
        logger.error(f"Error getting audit log: {e}")
        return _api_error("Security audit log is unavailable")

# HTML Template
HTML_TEMPLATE = r'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AVA - DevOps AI Agent v2.1.2</title>
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #1a1a1a;
            color: #e0e0e0;
            height: 100vh;
            overflow: hidden;
        }
        
        .app-container {
            display: flex;
            height: 100vh;
        }
        
        /* Sidebar */
        .sidebar {
            width: 260px;
            background: #171717;
            border-right: 1px solid #2a2a2a;
            display: flex;
            flex-direction: column;
            padding: 16px;
                    position: relative;
        }
        
        .sidebar-header {
            padding: 12px 16px;
            margin-bottom: 20px;
        }
        
        .sidebar-title {
            font-size: 20px;
            font-weight: 600;
            color: #fff;
        }
        
        .sidebar-btn {
            width: 100%;
            padding: 10px 16px;
            background: #2a2a2a;
            border: 1px solid #3a3a3a;
            border-radius: 8px;
            color: #e0e0e0;
            font-size: 14px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 8px;
            transition: all 0.2s;
        }
        
        .sidebar-btn:hover {
            background: #333;
        }
        
        .sidebar-section {
            margin-top: 24px;
            flex: 1;
            overflow-y: auto;
        }
        
        .sidebar-section-title {
            font-size: 12px;
            font-weight: 600;
            color: #888;
            text-transform: uppercase;
            padding: 8px 16px;
            margin-bottom: 8px;
        }
        
        .chat-item {
            padding: 10px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            color: #aaa;
            transition: all 0.2s;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .chat-item:hover {
            background: #2a2a2a;
            color: #fff;
        }

        .thread-list {
            margin-top: 18px;
            flex: 1;
            min-height: 0;
            overflow-y: auto;
            padding: 0 2px 18px 0;
            scrollbar-gutter: stable;
        }

        .thread-list-title {
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-faint);
            margin-bottom: 10px;
            padding: 0 4px;
        }

        .thread-list .chat-item {
            display: flex;
            flex-direction: column;
            gap: 4px;
            padding: 12px 14px;
            margin-bottom: 8px;
            white-space: normal;
            line-height: 1.45;
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.04);
            border-radius: 14px;
        }

        .thread-query {
            color: #ececec;
            font-size: 13px;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }

        .thread-meta {
            color: var(--text-faint);
            font-size: 11px;
        }
        
        /* Modal */
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            animation: fadeIn 0.2s;
        }
        
        .modal-content {
            background: #1f1f1f;
            margin: 5% auto;
            padding: 0;
            border: 1px solid #3a3a3a;
            border-radius: 12px;
            width: 90%;
            max-width: 700px;
            max-height: 80vh;
            overflow: hidden;
            animation: slideUp 0.3s;
        }

        .modal.side-panel {
            background: rgba(0, 0, 0, 0.64);
        }

        .modal.side-panel .modal-content {
            margin: 0 0 0 auto;
            width: min(460px, 100%);
            max-width: 460px;
            height: 100vh;
            max-height: 100vh;
            border-radius: 22px 0 0 22px;
            animation: slideInPanel 0.24s ease;
        }

        @keyframes slideUp {
            from { transform: translateY(50px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }

        @keyframes slideInPanel {
            from { transform: translateX(24px); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        
        .modal-header {
            padding: 20px 24px;
            border-bottom: 1px solid #2a2a2a;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .modal-title {
            font-size: 18px;
            font-weight: 600;
        }
        
        .modal-close {
            background: none;
            border: none;
            color: #888;
            font-size: 24px;
            cursor: pointer;
            padding: 0;
            width: 32px;
            height: 32px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 6px;
            transition: all 0.2s;
        }
        
        .modal-close:hover {
            background: #2a2a2a;
            color: #fff;
        }
        
        .modal-body {
            padding: 24px;
            max-height: calc(80vh - 80px);
            overflow-y: auto;
        }
        
        .history-item {
            padding: 16px;
            background: #242424;
            border-radius: 8px;
            margin-bottom: 12px;
            border: 1px solid #2a2a2a;
        }
        
        .history-query {
            font-weight: 500;
            margin-bottom: 8px;
            color: #fff;
        }
        
        .history-meta {
            font-size: 12px;
            color: #888;
            display: flex;
            gap: 12px;
        }
        
        .stat-card {
            padding: 16px;
            background: #242424;
            border-radius: 8px;
            margin-bottom: 12px;
            border: 1px solid #2a2a2a;
        }
        
        .stat-label {
            font-size: 12px;
            color: #888;
            margin-bottom: 4px;
        }
        
        .stat-value {
            font-size: 24px;
            font-weight: 600;
            color: #667eea;
        }
        
        /* Main Content */
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: #1a1a1a;
        }
        
        .top-bar {
            padding: 16px 24px;
            border-bottom: 1px solid #2a2a2a;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        
        .stats {
            display: flex;
            gap: 16px;
            font-size: 13px;
            color: #888;
        }
        
        .stat-item {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        
        /* Chat Area */
        .chat-area {
            flex: 1;
            overflow-y: auto;
            padding: 24px;
            display: flex;
            flex-direction: column;
            gap: 24px;
        }
        
        .welcome-screen {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100%;
            text-align: center;
        }
        
        .welcome-title {
            font-size: 32px;
            font-weight: 600;
            margin-bottom: 12px;
        }
        
        .welcome-subtitle {
            font-size: 16px;
            color: #888;
            margin-bottom: 32px;
        }
        
        .example-prompts {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 12px;
            max-width: 800px;
            width: 100%;
            margin-top: 24px;
        }
        
        .example-prompt {
            padding: 16px;
            background: #242424;
            border: 1px solid #333;
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.2s;
            text-align: left;
        }
        
        .example-prompt:hover {
            background: #2a2a2a;
            border-color: #444;
        }
        
        .example-icon {
            font-size: 20px;
            margin-bottom: 8px;
        }
        
        .example-text {
            font-size: 14px;
            color: #ccc;
        }
        
        /* Messages */
        .message {
            display: flex;
            gap: 16px;
            max-width: 900px;
            margin: 0 auto;
            width: 100%;
            animation: fadeIn 0.3s ease;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .message-avatar {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            font-weight: 600;
            font-size: 14px;
        }
        
        .message-content {
            flex: 1;
            padding-top: 4px;
        }
        
        .message-header {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 8px;
        }
        
        .message-author {
            font-weight: 600;
            font-size: 14px;
        }
        
        .message-meta {
            font-size: 12px;
            color: #666;
        }
        
        .message-text {
            line-height: 1.7;
            font-size: 15px;
            color: #d0d0d0;
        }
        
        .message.user .message-text {
            color: #fff;
            font-weight: 500;
        }
        
        .message-actions {
            display: flex;
            gap: 8px;
            margin-top: 12px;
        }
        
        .action-btn {
            padding: 6px 12px;
            background: #2a2a2a;
            border: 1px solid #3a3a3a;
            border-radius: 6px;
            color: #aaa;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .action-btn:hover {
            background: #333;
            color: #fff;
        }
        
        /* Code blocks */
        pre {
            background: #0d0d0d;
            border: 1px solid #2a2a2a;
            border-radius: 8px;
            padding: 16px;
            overflow-x: auto;
            margin: 12px 0;
        }
        
        code {
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 13px;
            line-height: 1.6;
        }
        
        /* Input Area */
        .input-container {
            border-top: 1px solid #2a2a2a;
            padding: 20px 24px;
            background: #1a1a1a;
        }
        
        .input-wrapper {
            max-width: 900px;
            margin: 0 auto;
            position: relative;
        }
        
        .input-box {
            display: flex;
            align-items: flex-end;
            background: #242424;
            border: 1px solid #3a3a3a;
            border-radius: 12px;
            padding: 12px;
            transition: all 0.2s;
        }
        
        .input-box:focus-within {
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .input-actions {
            display: flex;
            gap: 8px;
            margin-right: 8px;
        }
        
        .input-action-btn {
            width: 32px;
            height: 32px;
            border-radius: 8px;
            background: #2a2a2a;
            border: none;
            color: #888;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
        }
        
        .input-action-btn:hover {
            background: #333;
            color: #fff;
        }
        
        #queryInput {
            flex: 1;
            background: transparent;
            border: none;
            outline: none;
            color: #fff;
            font-size: 15px;
            font-family: 'Inter', sans-serif;
            resize: none;
            min-height: 24px;
            max-height: 200px;
            line-height: 1.5;
        }
        
        #queryInput:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        .send-btn {
            width: 36px;
            height: 36px;
            border-radius: 8px;
            background: #667eea;
            border: none;
            color: #fff;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
            flex-shrink: 0;
        }
        
        .send-btn:hover {
            background: #5568d3;
        }
        
        .send-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        .disclaimer {
            text-align: center;
            font-size: 12px;
            color: #666;
            margin-top: 12px;
            max-width: 900px;
            margin-left: auto;
            margin-right: auto;
        }
        
        /* Loading */
        .loading-message {
            display: flex;
            gap: 16px;
            max-width: 900px;
            margin: 0 auto;
            width: 100%;
        }
        
        .loading-dots {
            display: flex;
            gap: 4px;
            padding: 12px 0;
        }
        
        .loading-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #667eea;
            animation: bounce 1.4s infinite ease-in-out;
        }
        
        .loading-dot:nth-child(1) { animation-delay: -0.32s; }
        .loading-dot:nth-child(2) { animation-delay: -0.16s; }
        
        @keyframes bounce {
            0%, 80%, 100% { transform: scale(0); }
            40% { transform: scale(1); }
        }
        
        /* Scrollbar */
        ::-webkit-scrollbar {
            width: 6px;
            height: 6px;
        }
        
        ::-webkit-scrollbar-track {
            background: #1a1a1a;
        }
        
        ::-webkit-scrollbar-thumb {
            background: #3a3a3a;
            border-radius: 3px;
        }
        
        ::-webkit-scrollbar-thumb:hover {
            background: #4a4a4a;
        }
        
        /* Hidden file input */
        #fileInput {
            display: none;
        }
        
        /* Security Badge & Dashboard Styles */
        .badge {
            background: #ff4444;
            color: white;
            border-radius: 10px;
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 600;
            margin-left: auto;
        }
        
        .security-stat {
            display: flex;
            justify-content: space-between;
            padding: 12px 16px;
            background: #242424;
            border-radius: 8px;
            margin-bottom: 8px;
        }
        
        .security-stat-label {
            color: #aaa;
            font-size: 14px;
        }
        
        .security-stat-value {
            color: #667eea;
            font-weight: 600;
            font-size: 14px;
        }
        
        .security-stat-value.danger {
            color: #ff6b6b;
        }
        
        .security-stat-value.success {
            color: #51cf66;
        }
        
        .audit-entry {
            padding: 12px;
            background: #242424;
            border-radius: 6px;
            margin-bottom: 8px;
            border-left: 3px solid #667eea;
            font-size: 13px;
        }
        
        .audit-entry.blocked {
            border-left-color: #ff6b6b;
        }
        
        .audit-entry.executed {
            border-left-color: #51cf66;
        }
        
        .audit-time {
            color: #888;
            font-size: 11px;
            margin-bottom: 4px;
        }
        
        .audit-command {
            color: #fff;
            font-family: 'Monaco', monospace;
            margin-bottom: 4px;
        }
        
        .audit-risk {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            margin-right: 8px;
        }
        
        .audit-risk.high {
            background: #ff6b6b22;
            color: #ff6b6b;
        }
        
        .audit-risk.low {
            background: #51cf6622;
            color: #51cf66;
        }

        /* Codex-inspired shell overrides */
        :root {
            --bg-app: #141414;
            --bg-panel: #191919;
            --bg-panel-2: #1f1f1f;
            --bg-panel-3: #262626;
            --bg-panel-4: #2d2d2d;
            --border-soft: #2f2f2f;
            --border-strong: #3a3a3a;
            --text-main: #f2f2f2;
            --text-muted: #9b9b9b;
            --text-faint: #737373;
            --accent: #7c8cff;
            --accent-soft: rgba(124, 140, 255, 0.14);
            --danger: #ff6b6b;
            --danger-soft: rgba(255, 107, 107, 0.12);
            --warning: #f2c572;
            --warning-soft: rgba(242, 197, 114, 0.12);
            --success: #63d297;
            --success-soft: rgba(99, 210, 151, 0.12);
            --shadow-shell: 0 18px 50px rgba(0, 0, 0, 0.28);
        }

        body {
            background:
                radial-gradient(circle at top, rgba(124, 140, 255, 0.08), transparent 26%),
                linear-gradient(180deg, #171717 0%, #121212 100%);
            color: var(--text-main);
        }

        .app-container {
            padding: 16px;
            gap: 16px;
            background: transparent;
        }

        .sidebar {
            position: relative;
            display: flex;
            flex-direction: column;
            width: 284px;
            height: calc(100vh - 32px);
            max-height: calc(100vh - 32px);
            min-height: 0;
            padding: 16px 14px 148px;
            background: rgba(20, 20, 20, 0.94);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 22px;
            box-shadow: var(--shadow-shell);
            backdrop-filter: blur(12px);
            overflow: hidden;
        }

        .sidebar-header {
            padding: 8px 8px 14px;
            margin-bottom: 14px;
        }

        .sidebar-title {
            font-size: 29px;
            font-weight: 600;
            letter-spacing: -0.04em;
        }

        .sidebar-subtitle {
            margin-top: 6px;
            color: var(--text-faint);
            font-size: 13px;
            line-height: 1.5;
        }

        .sidebar-btn {
            padding: 12px 14px;
            background: transparent;
            border: 1px solid transparent;
            border-radius: 14px;
            color: #d5d5d5;
            font-size: 15px;
            font-weight: 500;
        }

        .sidebar-btn:hover {
            background: rgba(255, 255, 255, 0.05);
            border-color: rgba(255, 255, 255, 0.06);
        }

        .nav-icon {
            width: 18px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: #c8c8c8;
            font-size: 15px;
        }

        .profile-card {
            margin-top: 12px;
            padding: 12px 14px;
            background: linear-gradient(180deg, rgba(124, 140, 255, 0.08), rgba(124, 140, 255, 0.03));
            border: 1px solid rgba(124, 140, 255, 0.18);
            border-radius: 16px;
        }

        .profile-row {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .profile-avatar {
            width: 34px;
            height: 34px;
            border-radius: 12px;
            background: linear-gradient(180deg, #8a96ff 0%, #7481f4 100%);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: 700;
            flex-shrink: 0;
        }

        .profile-meta {
            min-width: 0;
            flex: 1;
        }

        .profile-name {
            color: #f3f3f3;
            font-size: 13px;
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .profile-role {
            display: inline-flex;
            margin-top: 4px;
            padding: 3px 8px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.06);
            color: #c5ccff;
            font-size: 11px;
        }

        .profile-power {
            background: none;
            border: none;
            color: #7f7f7f;
            cursor: pointer;
            font-size: 15px;
            padding: 4px;
            border-radius: 8px;
        }

        .profile-power:hover {
            background: rgba(255, 255, 255, 0.05);
            color: var(--danger);
        }

        .sidebar-quick {
            margin-top: 18px;
            padding: 14px;
            border-radius: 18px;
            background: rgba(255, 255, 255, 0.025);
            border: 1px solid rgba(255, 255, 255, 0.05);
            flex-shrink: 0;
        }

        .sidebar-quick-title {
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-faint);
            margin-bottom: 12px;
        }

        .quick-stat-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .quick-stat {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
            color: #d8d8d8;
        }

        .quick-stat-value {
            color: #f0f0f0;
            font-weight: 600;
        }

        .main-content {
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 26px;
            overflow: hidden;
            background: rgba(18, 18, 18, 0.92);
            box-shadow: var(--shadow-shell);
            backdrop-filter: blur(10px);
        }

        .workspace-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 16px 22px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            background: linear-gradient(180deg, rgba(255, 255, 255, 0.02), rgba(255, 255, 255, 0));
        }

        .mobile-topbar {
            display: none;
            align-items: center;
            justify-content: space-between;
            padding: 14px 16px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            background: rgba(18, 18, 18, 0.96);
        }

        .mobile-topbar-title {
            font-size: 15px;
            font-weight: 600;
        }

        .mobile-topbar-btn {
            width: 38px;
            height: 38px;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            background: rgba(255, 255, 255, 0.03);
            color: #efefef;
            cursor: pointer;
        }

        .sidebar-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.56);
            z-index: 999;
        }

        .workspace-title {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .workspace-title h1 {
            font-size: 16px;
            font-weight: 600;
            letter-spacing: -0.02em;
        }

        .workspace-title p {
            font-size: 12px;
            color: var(--text-faint);
        }

        .workspace-pills {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }

        .workspace-pill {
            padding: 6px 10px;
            border-radius: 999px;
            border: 1px solid rgba(255, 255, 255, 0.07);
            background: rgba(255, 255, 255, 0.03);
            color: var(--text-muted);
            font-size: 12px;
            white-space: nowrap;
        }

        .workspace-pill.primary {
            color: #dfe3ff;
            background: var(--accent-soft);
            border-color: rgba(124, 140, 255, 0.28);
        }

        .security-inline-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 18px;
            height: 18px;
            padding: 0 6px;
            margin-left: auto;
            border-radius: 999px;
            background: #ff5a5a;
            color: white;
            font-size: 11px;
            font-weight: 700;
        }

        .chat-area {
            padding: 28px 28px 18px;
            gap: 30px;
        }

        .welcome-screen {
            max-width: 920px;
            margin: 0 auto;
            align-items: flex-start;
            justify-content: center;
            text-align: left;
        }

        .welcome-title {
            font-size: 42px;
            line-height: 1.05;
            letter-spacing: -0.05em;
            margin-bottom: 10px;
        }

        .welcome-subtitle {
            color: var(--text-muted);
            max-width: 680px;
            margin-bottom: 18px;
        }

        .example-prompts {
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            max-width: 760px;
            margin-top: 16px;
        }

        .example-prompt {
            background: rgba(255, 255, 255, 0.025);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 18px;
            padding: 16px 18px;
        }

        .example-prompt:hover {
            background: rgba(255, 255, 255, 0.04);
            border-color: rgba(255, 255, 255, 0.08);
            transform: translateY(-1px);
        }

        .message,
        .loading-message {
            max-width: 980px;
        }

        .message-avatar {
            width: 42px;
            height: 42px;
            border-radius: 14px;
            background: linear-gradient(180deg, #7f8cf7 0%, #6a6ff0 100%);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.12);
        }

        .message.user .message-avatar {
            background: linear-gradient(180deg, #5957d6 0%, #716af0 100%);
        }

        .message-content {
            max-width: 820px;
            padding-top: 2px;
        }

        .message-header {
            margin-bottom: 10px;
        }

        .message-author {
            font-size: 15px;
        }

        .message-meta {
            color: var(--text-faint);
        }

        .message-text {
            color: #dfdfdf;
            line-height: 1.82;
            font-size: 15px;
        }

        .message.user .message-text {
            display: inline-block;
            padding: 12px 16px;
            border-radius: 20px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.06);
            color: #f6f6f6;
            font-weight: 400;
        }

        .message-actions {
            margin-top: 14px;
        }

        .action-btn {
            padding: 7px 12px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.03);
            border-color: rgba(255, 255, 255, 0.06);
            color: var(--text-muted);
        }

        .action-btn:hover {
            background: rgba(255, 255, 255, 0.06);
            color: var(--text-main);
        }

        .status-card {
            padding: 16px 18px;
            border-radius: 18px;
            border: 1px solid var(--border-soft);
            background: var(--bg-panel-2);
        }

        .status-card-title {
            font-size: 13px;
            font-weight: 700;
            margin-bottom: 10px;
            letter-spacing: 0.01em;
        }

        .status-card-copy {
            color: #d9d9d9;
            font-size: 14px;
            line-height: 1.7;
        }

        .status-card-meta {
            margin-top: 10px;
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }

        .status-chip {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 5px 9px;
            border-radius: 999px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            background: rgba(255, 255, 255, 0.03);
            color: var(--text-muted);
            font-size: 12px;
        }

        .status-card.blocked {
            background: var(--danger-soft);
            border-color: rgba(255, 107, 107, 0.28);
        }

        .status-card.blocked .status-card-title {
            color: #ff8a8a;
        }

        .status-card.approval {
            background: var(--warning-soft);
            border-color: rgba(242, 197, 114, 0.25);
        }

        .status-card.approval .status-card-title {
            color: #ffd990;
        }

        .status-card.executed {
            background: rgba(255, 255, 255, 0.02);
            border-color: rgba(255, 255, 255, 0.07);
        }

        .terminal-block {
            margin-top: 10px;
            background: #111;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 16px;
            overflow: hidden;
        }

        .terminal-label {
            padding: 10px 14px;
            font-size: 12px;
            color: var(--text-faint);
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            background: rgba(255, 255, 255, 0.02);
        }

        .findings-block {
            margin-top: 12px;
            display: grid;
            gap: 10px;
        }

        .findings-panel {
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 14px;
            background: rgba(255, 255, 255, 0.03);
            overflow: hidden;
        }

        .findings-label {
            padding: 10px 14px;
            font-size: 12px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: rgba(255, 255, 255, 0.58);
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
        }

        .findings-list {
            list-style: none;
            margin: 0;
            padding: 12px 14px;
            display: grid;
            gap: 10px;
        }

        .findings-item {
            color: #e6e6e6;
            font-size: 13px;
            line-height: 1.55;
        }

        .findings-item-title {
            color: #ffffff;
            font-weight: 600;
            margin-bottom: 2px;
        }

        .findings-item-copy {
            color: rgba(255, 255, 255, 0.74);
        }

        .findings-item-subcopy {
            color: rgba(255, 255, 255, 0.56);
            margin-top: 4px;
            font-size: 12px;
        }

        .findings-actions {
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }

        .findings-action-btn {
            appearance: none;
            border: 1px solid rgba(132, 146, 255, 0.28);
            background: rgba(103, 115, 225, 0.12);
            color: #dfe5ff;
            border-radius: 999px;
            padding: 7px 12px;
            font-size: 12px;
            line-height: 1;
            cursor: pointer;
            transition: background 0.18s ease, border-color 0.18s ease, transform 0.18s ease;
        }

        .findings-action-btn:hover {
            background: rgba(103, 115, 225, 0.2);
            border-color: rgba(132, 146, 255, 0.42);
            transform: translateY(-1px);
        }

        .findings-action-btn:disabled {
            opacity: 0.45;
            cursor: not-allowed;
            transform: none;
        }

        .terminal-output {
            margin: 0;
            padding: 16px 18px;
            background: transparent;
            border: none;
            border-radius: 0;
        }

        .terminal-output code {
            color: #aef1c8;
        }

        .input-container {
            padding: 18px 24px 22px;
            background: linear-gradient(180deg, rgba(18, 18, 18, 0), rgba(18, 18, 18, 0.96) 18%);
        }

        .input-wrapper {
            max-width: 980px;
        }

        .input-box {
            padding: 16px 18px 12px;
            border-radius: 24px;
            border-color: rgba(255, 255, 255, 0.08);
            background: rgba(41, 41, 41, 0.88);
            min-height: 92px;
            flex-wrap: wrap;
            gap: 10px;
        }

        .input-box:focus-within {
            border-color: rgba(124, 140, 255, 0.4);
            box-shadow: 0 0 0 4px rgba(124, 140, 255, 0.08);
        }

        .input-actions {
            margin-right: 0;
            align-self: flex-end;
        }

        #queryInput {
            min-height: 44px;
            font-size: 17px;
        }

        .send-btn {
            width: 44px;
            height: 44px;
            border-radius: 14px;
            background: linear-gradient(180deg, #8a96ff 0%, #7481f4 100%);
        }

        .composer-meta {
            display: flex;
            align-items: center;
            gap: 10px;
            width: 100%;
            padding-left: 42px;
            margin-top: 2px;
        }

        .composer-pill {
            padding: 5px 10px;
            border-radius: 999px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            background: rgba(255, 255, 255, 0.025);
            color: var(--text-faint);
            font-size: 12px;
        }

        .sidebar-footer {
            position: absolute;
            left: 14px;
            right: 14px;
            bottom: 12px;
            padding-top: 12px;
            border-top: 1px solid rgba(255, 255, 255, 0.06);
            background: linear-gradient(180deg, rgba(20, 20, 20, 0), rgba(20, 20, 20, 0.92) 18%, rgba(20, 20, 20, 1) 100%);
            z-index: 2;
        }

        .sidebar-footer-card {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px 12px;
            border-radius: 16px;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.05);
            cursor: pointer;
            min-height: 56px;
        }

        .sidebar-footer-card:hover {
            background: rgba(255, 255, 255, 0.05);
        }

        .sidebar-footer-text {
            min-width: 0;
            flex: 1;
            overflow: hidden;
        }

        .sidebar-footer-name {
            color: #f3f3f3;
            font-size: 13px;
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .sidebar-footer-role {
            color: var(--text-faint);
            font-size: 11px;
            margin-top: 2px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .disclaimer {
            margin-top: 10px;
            color: var(--text-faint);
        }

        .modal-content {
            background: rgba(28, 28, 28, 0.96);
            border-color: rgba(255, 255, 255, 0.08);
            border-radius: 24px;
        }

        .history-item,
        .stat-card,
        .security-stat,
        .audit-entry {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 16px;
        }

        @media (max-width: 980px) {
            .app-container {
                padding: 10px;
                gap: 10px;
            }

            .sidebar {
                width: 232px;
            }

            .example-prompts {
                grid-template-columns: 1fr;
            }

            .workspace-bar {
                padding: 14px 16px;
            }

            .chat-area {
                padding: 20px 16px 16px;
            }

            .input-container {
                padding: 14px 16px 18px;
            }
        }

        @media (max-width: 760px) {
            .app-container {
                padding: 0;
            }

            .sidebar {
                display: flex;
                position: fixed;
                top: 12px;
                bottom: 12px;
                left: 12px;
                width: min(88vw, 320px);
                height: auto;
                max-height: none;
                z-index: 1001;
                transform: translateX(calc(-100% - 18px));
                transition: transform 0.22s ease;
            }

            .sidebar.open {
                transform: translateX(0);
            }

            .sidebar-overlay.open {
                display: block;
            }

            .main-content {
                border-radius: 0;
                border-left: none;
                border-right: none;
            }

            .mobile-topbar {
                display: flex;
            }

            .workspace-bar {
                display: none;
            }

            .composer-meta {
                padding-left: 0;
                flex-wrap: wrap;
            }

            .modal.side-panel .modal-content {
                max-width: 100%;
                width: 100%;
                border-radius: 18px 18px 0 0;
                height: min(82vh, 82vh);
                max-height: min(82vh, 82vh);
                margin: auto 0 0 0;
            }
        }
    </style>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <script>mermaid.initialize({ startOnLoad: false, theme: 'dark' });</script>
</head>
<body>

    <!-- Login Overlay -->
    <div id="loginOverlay" style="display:flex;position:fixed;inset:0;background:rgba(10,10,20,0.97);z-index:9999;align-items:center;justify-content:center;font-family:Inter,sans-serif;">
        <div style="background:#12121f;border:1px solid #2a2a4a;border-radius:16px;padding:40px 48px;width:380px;box-shadow:0 24px 60px rgba(0,0,0,0.8);">
            <div style="text-align:center;margin-bottom:32px;">
                <div style="font-size:36px;margin-bottom:8px;">🤖</div>
                <div style="font-size:22px;font-weight:700;color:#e0e0e0;">AVA</div>
                <div style="font-size:13px;color:#667eea;margin-top:4px;">DevOps AI Agent — Secure Login</div>
            </div>
            <div style="margin-bottom:16px;">
                <label style="display:block;font-size:12px;color:#888;margin-bottom:6px;text-transform:uppercase;">Username</label>
                <input id="loginUsername" type="text" placeholder="admin"
                    style="width:100%;box-sizing:border-box;padding:10px 14px;background:#1a1a2e;border:1px solid #2a2a4a;border-radius:8px;color:#e0e0e0;font-size:14px;outline:none;"
                    onkeydown="if(event.key==='Enter') loginSubmit()">
            </div>
            <div style="margin-bottom:24px;">
                <label style="display:block;font-size:12px;color:#888;margin-bottom:6px;text-transform:uppercase;">Password</label>
                <input id="loginPassword" type="password" placeholder="••••••••"
                    style="width:100%;box-sizing:border-box;padding:10px 14px;background:#1a1a2e;border:1px solid #2a2a4a;border-radius:8px;color:#e0e0e0;font-size:14px;outline:none;"
                    onkeydown="if(event.key==='Enter') loginSubmit()">
            </div>
            <div id="loginError" style="display:none;color:#ff6b6b;font-size:13px;margin-bottom:16px;text-align:center;"></div>
            <button onclick="loginSubmit()" id="loginBtn" style="width:100%;padding:12px;background:linear-gradient(135deg,#667eea,#764ba2);border:none;border-radius:8px;color:white;font-size:15px;font-weight:600;cursor:pointer;">
                Sign In
            </button>
            <div style="text-align:center;margin-top:20px;font-size:11px;color:#444;">Tokens expire after 24 hours</div>
        </div>
    </div>
    <!-- End Login Overlay -->

    <div id="sidebarOverlay" class="sidebar-overlay" onclick="closeSidebar()"></div>
    <div class="app-container">
        <!-- Sidebar -->
        <div class="sidebar" id="sidebar">
            <div class="sidebar-header">
                <div class="sidebar-title">AVA</div>
                <div class="sidebar-subtitle">Unified DevOps assistant with grounded answers, secured actions, and approval-aware execution.</div>
                <div class="profile-card">
                    <div class="profile-row">
                        <div class="profile-avatar" id="sessionAvatar">A</div>
                        <div class="profile-meta">
                            <div class="profile-name" id="sessionBadgeName">admin</div>
                            <div class="profile-role" id="sessionBadgeRole">admin</div>
                        </div>
                        <button class="profile-power" onclick="logoutAva()" title="Sign out">&#x23FB;</button>
                    </div>
                </div>
            </div>
            
            <button class="sidebar-btn" onclick="newChat()">
                <span class="nav-icon">+</span>
                <span>New chat</span>
            </button>
            
            <button class="sidebar-btn" onclick="showHistoryModal()">
                <span class="nav-icon">◷</span>
                <span>History</span>
            </button>
            
            <button class="sidebar-btn" onclick="showStatsModal()">
                <span class="nav-icon">◫</span>
                <span>Stats</span>
            </button>
            
            <button class="sidebar-btn" onclick="showSettingsModal()">
                <span class="nav-icon">⚙</span>
                <span>Settings</span>
            </button>
            
            <button class="sidebar-btn" onclick="showSecurityModal()">
                <span class="nav-icon">⛨</span>
                <span>Security</span>
                <span id="securityBadge" class="security-inline-badge" style="display: none;"></span>
            </button>

            <div class="sidebar-quick">
                <div class="sidebar-quick-title">Quick Status</div>
                <div class="quick-stat-list">
                    <div class="quick-stat">
                        <span>Approval queue</span>
                        <span class="quick-stat-value" id="quickApprovalCount">0</span>
                    </div>
                    <div class="quick-stat">
                        <span>Blocked today</span>
                        <span class="quick-stat-value" id="quickBlockedCount">0</span>
                    </div>
                    <div class="quick-stat">
                        <span>Commands today</span>
                        <span class="quick-stat-value" id="quickCommandCount">0</span>
                    </div>
                </div>
            </div>
            
            <!-- Recent Chats removed from sidebar — use History button instead -->
            <div class="thread-list">
                <div class="thread-list-title">Recent Threads</div>
                <div id="recentChats"></div>
            </div>

            <!-- Sidebar footer -->
            <div class="sidebar-footer">
                <div class="sidebar-footer-card" onclick="logoutAva()" title="Click to sign out">
                    <div class="profile-avatar" id="userAvatar">A</div>
                    <div class="sidebar-footer-text">
                        <div class="sidebar-footer-name" id="userBadgeName">admin</div>
                        <div class="sidebar-footer-role" id="userBadgeRole">Signed in</div>
                    </div>
                    <div style="color:#595959; font-size:14px;">&#x23FB;</div>
                </div>
            </div>
        </div>
        
        <!-- Main Content -->
        <div class="main-content">
            <div class="mobile-topbar">
                <button class="mobile-topbar-btn" onclick="openSidebar()">☰</button>
                <div class="mobile-topbar-title">Project Ava</div>
                <button class="mobile-topbar-btn" onclick="newChat()">＋</button>
            </div>
            <div class="workspace-bar">
                <div class="workspace-title">
                    <h1>Project Ava</h1>
                    <p>Single serving contract: exact answers, grounded DevOps knowledge, and secured action handling.</p>
                </div>
                <div class="workspace-pills">
                    <span class="workspace-pill primary">AVA Runtime</span>
                    <span class="workspace-pill">Qwen 2.5 14B</span>
                    <span class="workspace-pill">Approval Guard</span>
                </div>
            </div>
            
            <!-- Chat Area -->
            
            <div class="chat-area" id="chatArea">
                <div class="welcome-screen" id="welcomeScreen">
                    <div class="welcome-title">Operate infrastructure through one assistant.</div>
                    <div class="welcome-subtitle">Ask for exact operational checks, grounded DevOps explanations, or secured actions. AVA will execute, request approval, or block when policy requires it.</div>
                    
                    <div class="example-prompts">
                        <div class="example-prompt" onclick="askExample('verify my system')">
                            <div class="example-icon">🧭</div>
                            <div class="example-text">Verify system state</div>
                        </div>
                        <div class="example-prompt" onclick="askExample('check docker')">
                            <div class="example-icon">🐳</div>
                            <div class="example-text">Inspect Docker runtime</div>
                        </div>
                        <div class="example-prompt" onclick="askExample('restart docker service')">
                            <div class="example-icon">⚠️</div>
                            <div class="example-text">Queue an approval-required action</div>
                        </div>
                        <div class="example-prompt" onclick="askExample('What is the difference between readiness probe and liveness probe?')">
                            <div class="example-icon">📘</div>
                            <div class="example-text">Ask a grounded DevOps question</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="input-container">
                <div class="input-wrapper">
                    <div class="input-box">
                        <div class="input-actions">
                            <button class="input-action-btn" onclick="document.getElementById('fileInput').click()" title="Upload file">
                                <span>+</span>
                            </button>
                            <input type="file" id="fileInput" accept=".tf,.yml,.yaml,.json,.sh,.py,.md,.hcl" onchange="handleFileUpload(this)">
                        </div>
                        <textarea id="queryInput" placeholder="Ask anything..." rows="1" onkeydown="handleKeyPress(event)" oninput="autoResize(this)"></textarea>
                        <button class="send-btn" onclick="sendQuery()" id="sendBtn">
                            <span>→</span>
                        </button>
                        <div class="composer-meta">
                            <span class="composer-pill">Local</span>
                            <span class="composer-pill">Qwen 2.5 14B</span>
                            <span class="composer-pill">Approval-aware</span>
                        </div>
                    </div>
                    <div class="disclaimer">
                        AVA is a DevOps AI agent that can make mistakes. Verify important information.
                        <span style="color: #667eea; margin-left: 8px;">• Powered by Qwen 2.5 14B</span>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- History Modal -->
    <div id="historyModal" class="modal side-panel">
        <div class="modal-content">
            <div class="modal-header">
                <span class="modal-title">Query History</span>
                <button class="modal-close" onclick="closeModal('historyModal')">&times;</button>
            </div>
            <div class="modal-body" id="historyContent">
                <p style="color: #888;">Loading...</p>
            </div>
        </div>
    </div>
    
    <!-- Stats Modal -->
    <div id="statsModal" class="modal side-panel">
        <div class="modal-content">
            <div class="modal-header">
                <span class="modal-title">Knowledge Base Stats</span>
                <button class="modal-close" onclick="closeModal('statsModal')">&times;</button>
            </div>
            <div class="modal-body" id="statsContent">
                <p style="color: #888;">Loading...</p>
            </div>
        </div>
    </div>
    
    <!-- Settings Modal -->
    <div id="settingsModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <span class="modal-title">Settings</span>
                <button class="modal-close" onclick="closeModal('settingsModal')">&times;</button>
            </div>
            <div class="modal-body">
                <!-- Theme Toggle -->
                <div class="stat-card" style="display:flex; align-items:center; justify-content:space-between;">
                    <div>
                        <div class="stat-label">Theme</div>
                        <div id="themeLabel" style="color:#fff; font-size:13px; margin-top:4px;">Dark Mode</div>
                    </div>
                    <label style="position:relative; display:inline-block; width:48px; height:26px; cursor:pointer;">
                        <input type="checkbox" id="themeToggle" onchange="toggleTheme(this.checked)"
                            style="opacity:0; width:0; height:0;">
                        <span id="themeSlider" style="
                            position:absolute; inset:0; background:#2a2a4a;
                            border-radius:26px; transition:0.3s;
                            display:flex; align-items:center; padding:0 4px;
                            font-size:14px;
                        ">🌙</span>
                    </label>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Model</div>
                    <div style="color: #fff;">Qwen 2.5 14B (Local)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Security</div>
                    <div style="color: #fff;">OPA Policy Enforcement: Enabled</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Command Whitelist (Read-Only)</div>
                    <div style="color: #fff; font-size: 13px; line-height: 1.6;">
                        <strong>Basic:</strong> date, whoami, pwd, ls, cat, grep, df, free, ps, top, uptime, uname, echo, head, tail, wc, find, which, hostname<br>
                        <strong>Server:</strong> ollama, docker, systemctl, git, curl, wget, netstat, ss
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    
    <!-- Security Modal -->
    <div id="securityModal" class="modal side-panel">
        <div class="modal-content">
            <div class="modal-header">
                <span class="modal-title">🛡️ Security Dashboard</span>
                <button class="modal-close" onclick="closeModal('securityModal')">&times;</button>
            </div>
            <div class="modal-body" id="securityContent">
                <p style="color: #888;">Loading...</p>
            </div>
        </div>
    </div>
    
    <script>
        // ── Day 5: Auth State + JWT Handling ──────────────────────────────────
        const AVA_TOKEN_KEY = 'ava_jwt_token';

        function getToken() {
            return localStorage.getItem(AVA_TOKEN_KEY);
        }

        function setToken(token) {
            localStorage.setItem(AVA_TOKEN_KEY, token);
        }

        function clearToken() {
            localStorage.removeItem(AVA_TOKEN_KEY);
        }

        function showLoginOverlay(errorMsg) {
            document.getElementById('loginOverlay').style.display = 'flex';
            if (errorMsg) {
                const err = document.getElementById('loginError');
                err.textContent = errorMsg;
                err.style.display = 'block';
            }
            document.getElementById('loginUsername').focus();
        }

        function hideLoginOverlay() {
            document.getElementById('loginOverlay').style.display = 'none';
        }

        async function loginSubmit() {
            const username = document.getElementById('loginUsername').value.trim();
            const password = document.getElementById('loginPassword').value;
            const btn      = document.getElementById('loginBtn');
            const err      = document.getElementById('loginError');

            if (!username || !password) {
                err.textContent = 'Username and password are required.';
                err.style.display = 'block';
                return;
            }

            btn.disabled    = true;
            btn.textContent = 'Signing in…';
            err.style.display = 'none';

            try {
                const resp = await window._fetchNoAuth('/auth/login', {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    JSON.stringify({ username, password })
                });

                const data = await resp.json();

                if (resp.ok) {
                    setToken(data.access_token);
                    window._avaRole = data.role;
                    window._avaUser = data.username;
                    hideLoginOverlay();
                    document.getElementById('loginPassword').value = '';
                    applyRoleUI(data.role);
                } else {
                    err.textContent = data.error || 'Login failed.';
                    err.style.display = 'block';
                    document.getElementById('loginPassword').value = '';
                }
            } catch (e) {
                err.textContent = 'Network error — is AVA running?';
                err.style.display = 'block';
            } finally {
                btn.disabled    = false;
                btn.textContent = 'Sign In';
            }
        }

        function logoutAva() {
            clearToken();
            window._avaRole = null;
            window._avaUser = null;
            showLoginOverlay();
        }

        function applyRoleUI(role) {
            // Update user badge
            const nameEl = document.getElementById('userBadgeName');
            const roleEl = document.getElementById('userBadgeRole');
            const sessionNameEl = document.getElementById('sessionBadgeName');
            const sessionRoleEl = document.getElementById('sessionBadgeRole');
            if (nameEl) nameEl.textContent = window._avaUser || '';
            if (sessionNameEl) sessionNameEl.textContent = window._avaUser || '';
            const avatarEl = document.getElementById('userAvatar');
            const sessionAvatarEl = document.getElementById('sessionAvatar');
            if (avatarEl && window._avaUser) avatarEl.textContent = window._avaUser[0].toUpperCase();
            if (sessionAvatarEl && window._avaUser) sessionAvatarEl.textContent = window._avaUser[0].toUpperCase();
            if (roleEl) {
                roleEl.textContent = role;
                roleEl.style.background = role === 'admin' ? '#1a3a1a' : '#2a2a4a';
                roleEl.style.color = role === 'admin' ? '#4caf50' : '#888';
            }
            if (sessionRoleEl) {
                sessionRoleEl.textContent = role;
            }
            // Disable execution buttons for readonly users
            if (role === 'readonly') {
                document.querySelectorAll('.admin-only').forEach(el => {
                    el.style.opacity = '0.4';
                    el.style.pointerEvents = 'none';
                    el.title = 'Admin access required';
                });
            }
        }

        // Global fetch interceptor — auto-attach Bearer token to every request
        window._fetchNoAuth = window.fetch.bind(window);   // keep raw fetch for login
        window.fetch = function(url, options = {}) {
            const token = getToken();
            if (token) {
                options.headers = Object.assign(
                    { 'Authorization': 'Bearer ' + token },
                    options.headers || {}
                );
            }
            return window._fetchNoAuth(url, options).then(resp => {
                if (resp.status === 401) {
                    clearToken();
                    showLoginOverlay('Session expired. Please log in again.');
                    return Promise.reject(new Error('Unauthorized'));
                }
                return resp;
            });
        };

        // On page load — verify stored token or show login
        (async function checkAuth() {
            const token = getToken();
            if (!token) {
                showLoginOverlay();
                loadSecurityData();
                fetch('/stats').then(r => r.json()).then(data => {
                    const quickCommandCount = document.getElementById('quickCommandCount');
                    if (quickCommandCount) quickCommandCount.textContent = data.query_count || 0;
                }).catch(() => {});
                return;
            }
            try {
                const resp = await window._fetchNoAuth('/auth/me', {
                    headers: { 'Authorization': 'Bearer ' + token }
                });
                if (resp.ok) {
                    const data = await resp.json();
                    window._avaRole = data.role;
                    window._avaUser = data.username;
                    hideLoginOverlay();
                    applyRoleUI(data.role);
                } else {
                    clearToken();
                    showLoginOverlay();
                }
            } catch {
                showLoginOverlay();
            }
            loadSecurityData();
            fetch('/stats').then(r => r.json()).then(data => {
                const quickCommandCount = document.getElementById('quickCommandCount');
                if (quickCommandCount) quickCommandCount.textContent = data.query_count || 0;
            }).catch(() => {});
        })();
        // ── End Auth ──────────────────────────────────────────────────────────

        // Global state
        let currentApprovalId = null;
        console.log('PORT 5002 - FRESH CACHE');
        
        // Global error handler
        window.addEventListener('error', function(e) {
            console.error('Global error:', e.message, e.filename, e.lineno);
        });
        
        // Wait for DOM to be ready
        document.addEventListener('DOMContentLoaded', function() {
            console.log('AVA: DOM loaded successfully');
            
            try {
                // Load recent chats
                loadRecentChats();
                console.log('AVA: Recent chats loaded');
            } catch (err) {
                console.error('AVA: Error during initialization:', err);
            }
        });
        
        // Stats are now loaded in the Stats modal on-demand
        
        function loadRecentChats() {
            fetch('/history')
                .then(r => r.json())
                .then(data => {
                    const container = document.getElementById('recentChats');
                    if (!container) {
                        console.warn('recentChats container not found');
                        return;
                    }
                    const recent = data.history.slice(-10).reverse();
                    if (recent.length === 0) {
                        container.innerHTML = '<div class="chat-item" style="color: #666;">No recent chats</div>';
                        return;
                    }
                    container.innerHTML = recent.map(h => 
                        `<div class="chat-item">
                            <div class="thread-query">${escapeHtml(h.query)}</div>
                            <div class="thread-meta">${escapeHtml(h.type || 'query')} • ${escapeHtml(h.time_taken || '')}</div>
                        </div>`
                    ).join('');
                })
                .catch(err => {
                    console.error('Error loading recent chats:', err);
                });
        }
        
        function newChat() {
            console.log('newChat called');
            try {
                closeSidebar();
                location.reload();
            } catch (err) {
                console.error('Error in newChat:', err);
            }
        }
        
        function showHistoryModal() {
            console.log('showHistoryModal called');
            try {
                closeSidebar();
                document.getElementById('historyModal').style.display = 'block';
                fetch('/history')
                    .then(r => r.json())
                    .then(data => {
                        const content = document.getElementById('historyContent');
                        if (data.history.length === 0) {
                            content.innerHTML = '<p style="color: #888; text-align: center;">No history yet</p>';
                            return;
                        }
                        content.innerHTML = data.history.reverse().map(h => `
                            <div class="history-item">
                                <div class="history-query">${h.query}</div>
                                <div class="history-meta">
                                    <span>${h.type}</span>
                                    <span>${h.time_taken || 'N/A'}</span>
                                    <span>${new Date(h.timestamp).toLocaleString()}</span>
                                </div>
                            </div>
                        `).join('');
                    })
                    .catch(err => console.error('Error loading history:', err));
            } catch (err) {
                console.error('Error in showHistoryModal:', err);
            }
        }
        
        function showStatsModal() {
            console.log('showStatsModal called');
            try {
                closeSidebar();
                document.getElementById('statsModal').style.display = 'block';
                fetch('/stats')
                    .then(r => r.json())
                    .then(data => {
                        const quickCommandCount = document.getElementById('quickCommandCount');
                        if (quickCommandCount) quickCommandCount.textContent = data.query_count || 0;
                        const content = document.getElementById('statsContent');
                        content.innerHTML = `
                            <div class="stat-card">
                                <div class="stat-label">📚 Knowledge Base</div>
                                <div class="stat-value">${data.total_chunks.toLocaleString()} chunks indexed</div>
                            </div>
                            <div class="stat-card">
                                <div class="stat-label">🔧 Source Repositories</div>
                                <div class="stat-value">${data.repos} GitHub repos</div>
                                <div style="color: #888; font-size: 13px; margin-top: 6px;">
                                    DevOps Exercises, AWS/Jenkins/Docker/Terraform Zero to Hero
                                </div>
                            </div>
                            <div class="stat-card">
                                <div class="stat-label">🤖 LLM Model</div>
                                <div class="stat-value" style="font-size: 18px;">${data.model}</div>
                                <div style="color: #888; font-size: 13px; margin-top: 6px;">
                                    14B parameters • Local inference via Ollama
                                </div>
                            </div>
                            <div class="stat-card">
                                <div class="stat-label">🔢 Token Usage (Current Session)</div>
                                <div class="stat-value">${data.total_tokens ? data.total_tokens.toLocaleString() : '0'} tokens</div>
                                <div style="color: #888; font-size: 13px; margin-top: 6px;">
                                    Queries: ${data.query_count || 0} • Avg: ${data.avg_tokens_per_query || 0} tokens/query
                                </div>
                            </div>
                            <div class="stat-card">
                                <div class="stat-label">🛡️ Security</div>
                                <div class="stat-value">${data.opa_enabled ? 'OPA Policy Enforcement Enabled' : 'Disabled'}</div>
                                <div style="color: #888; font-size: 13px; margin-top: 6px;">
                                    Whitelist: ${data.whitelisted_commands || 0} commands • Blocked paths enforced
                                </div>
                            </div>
                            <div class="stat-card">
                                <div class="stat-label">💾 Storage</div>
                                <div class="stat-value">ChromaDB Vector Store</div>
                                <div style="color: #888; font-size: 13px; margin-top: 6px;">
                                    Embedding: nomic-embed-text • Collection: devops_policies_v2
                                </div>
                            </div>
                        `;
                    })
                    .catch(err => console.error('Error loading stats:', err));
            } catch (err) {
                console.error('Error in showStatsModal:', err);
            }
        }
        
        function showSettingsModal() {
            console.log('showSettingsModal called');
            try {
                closeSidebar();
                document.getElementById('settingsModal').style.display = 'block';
            } catch (err) {
                console.error('Error in showSettingsModal:', err);
            }
        }
        
        function closeModal(modalId) {
            console.log('closeModal called:', modalId);
            try {
                document.getElementById(modalId).style.display = 'none';
            } catch (err) {
                console.error('Error in closeModal:', err);
            }
        }

        function openSidebar() {
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('sidebarOverlay');
            if (sidebar) sidebar.classList.add('open');
            if (overlay) overlay.classList.add('open');
        }

        function closeSidebar() {
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('sidebarOverlay');
            if (sidebar) sidebar.classList.remove('open');
            if (overlay) overlay.classList.remove('open');
        }
        
        // Close modal on outside click
        window.onclick = function(event) {
            if (event.target.classList.contains('modal')) {
                event.target.style.display = 'none';
            }
        }
        
        function autoResize(textarea) {
            textarea.style.height = 'auto';
            textarea.style.height = textarea.scrollHeight + 'px';
        }
        
        function handleKeyPress(event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendQuery();
            }
        }
        
        function askExample(query) {
            document.getElementById('queryInput').value = query;
            sendQuery();
        }

        function submitSuggestedPrompt(button) {
            const encodedPrompt = button && button.getAttribute('data-prompt');
            if (!encodedPrompt) return;
            try {
                const prompt = decodeURIComponent(encodedPrompt);
                if (!prompt) return;
                askExample(prompt);
            } catch (err) {
                console.error('Error submitting suggested prompt:', err);
            }
        }
        
        function hideWelcome() {
            const welcome = document.getElementById('welcomeScreen');
            if (welcome) welcome.style.display = 'none';
        }
        
        function addUserMessage(text) {
            hideWelcome();
            const chatArea = document.getElementById('chatArea');
            const messageDiv = document.createElement('div');
            messageDiv.className = 'message user';
            messageDiv.innerHTML = `
                <div class="message-avatar">You</div>
                <div class="message-content">
                    <div class="message-text">${escapeHtml(text)}</div>
                </div>
            `;
            chatArea.appendChild(messageDiv); setTimeout(renderMermaidPlaceholders, 100); setTimeout(renderMermaidPlaceholders, 400);
            chatArea.scrollTop = chatArea.scrollHeight;
        }
        
        function addLoadingMessage(action = 'thinking') {
            const chatArea = document.getElementById('chatArea');
            const loadingDiv = document.createElement('div');
            loadingDiv.className = 'loading-message';
            loadingDiv.id = 'loadingMessage';
            
            const actionText = {
                'thinking': 'Thinking',
                'executing': 'Executing command',
                'analyzing': 'Analyzing file',
                'searching': 'Searching knowledge base'
            }[action] || 'Processing';
            
            loadingDiv.innerHTML = `
                <div class="message-avatar" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);">AVA</div>
                <div class="message-content">
                    <div style="color: #888; font-size: 13px; margin-bottom: 4px;">${actionText}...</div>
                    <div class="loading-dots">
                        <div class="loading-dot"></div>
                        <div class="loading-dot"></div>
                        <div class="loading-dot"></div>
                    </div>
                </div>
            `;
            chatArea.appendChild(loadingDiv);
            chatArea.scrollTop = chatArea.scrollHeight;
        }
        
        function removeLoadingMessage() {
            const loading = document.getElementById('loadingMessage');
            if (loading) loading.remove();
        }

        function renderCommandCard(result) {
            const normalized = result && typeof result === 'object' ? result : {};
            const metadata = normalized.metadata && typeof normalized.metadata === 'object' ? normalized.metadata : {};
            const risk = escapeHtml(
                ((normalized.security && normalized.security.risk) || normalized.risk || 'unknown').toString()
            );
            const approvalId = escapeHtml(normalized.approval_id || '');
            const command = escapeHtml(normalized.command || normalized.command_repr || 'requested action');
            const reason = escapeHtml(normalized.reason || normalized.error || 'Action could not be completed.');
            const output = escapeHtml(normalized.output || 'Command executed successfully');
            const alerts = Array.isArray(metadata.alerts) ? metadata.alerts : [];
            const suggestedActions = Array.isArray(metadata.suggested_actions) ? metadata.suggested_actions : [];
            const remediationCandidates = Array.isArray(metadata.remediation_candidates) ? metadata.remediation_candidates : [];
            const newListeners = Array.isArray(metadata.new_listeners) ? metadata.new_listeners : [];
            const newFailedServices = Array.isArray(metadata.new_failed_services) ? metadata.new_failed_services : [];
            const authFailureDelta = Number(metadata.auth_failure_delta || 0);
            const correlatedAssessment = metadata.correlated_assessment && typeof metadata.correlated_assessment === 'object' ? metadata.correlated_assessment : null;
            const primaryConcern = metadata.primary_concern && typeof metadata.primary_concern === 'object' ? metadata.primary_concern : null;
            const investigationPlan = metadata.investigation_plan && typeof metadata.investigation_plan === 'object' ? metadata.investigation_plan : null;
            const remediationPlan = metadata.remediation_plan && typeof metadata.remediation_plan === 'object' ? metadata.remediation_plan : null;
            const runtimeScope = safeString(metadata.runtime_scope || '');
            const assessmentMode = safeString(metadata.assessment_mode || '');
            const complianceNote = safeString(metadata.compliance_note || '');
            const routeSource = safeString(metadata.route_source || metadata.source || '');

            const renderStringList = (title, items) => {
                if (!items.length) return '';
                const rows = items.map(item => `<li class="findings-item">${escapeHtml(String(item))}</li>`).join('');
                return `<div class="findings-panel">
                    <div class="findings-label">${escapeHtml(title)}</div>
                    <ul class="findings-list">${rows}</ul>
                </div>`;
            };

            const renderPrimaryConcern = (concern) => {
                if (!concern || !concern.title) return '';
                const severity = escapeHtml(String(concern.severity || 'unknown'));
                const confidence = escapeHtml(String(concern.confidence || 'medium'));
                const evidence = Array.isArray(concern.evidence) ? concern.evidence.slice(0, 3) : [];
                const nextAction = safeString(concern.next_action || '');
                const evidenceHtml = evidence.length
                    ? `<ul class="findings-list">${evidence.map(item => `<li class="findings-item">${escapeHtml(String(item))}</li>`).join('')}</ul>`
                    : '';
                const actionButton = nextAction && !nextAction.toLowerCase().startsWith('no urgent action')
                    ? `<div class="findings-actions">
                        <button class="findings-action-btn" data-prompt="${encodeURIComponent(nextAction)}" onclick="submitSuggestedPrompt(this)">Run next action</button>
                    </div>`
                    : '';
                return `<div class="findings-panel primary-concern-panel">
                    <div class="findings-label">Primary concern</div>
                    <div class="findings-item-title">${escapeHtml(String(concern.title))}</div>
                    <div class="status-card-meta" style="margin: 10px 0 8px 0;">
                        <span class="status-chip">Severity: ${severity}</span>
                        <span class="status-chip">Confidence: ${confidence}</span>
                    </div>
                    ${evidenceHtml}
                    ${nextAction ? `<div class="findings-item-copy" style="margin-top: 10px;">Next action: ${escapeHtml(nextAction)}</div>` : ''}
                    ${actionButton}
                </div>`;
            };

            const renderCorrelatedAssessment = (assessment) => {
                if (!assessment || !assessment.title) return '';
                const severity = escapeHtml(String(assessment.severity || 'unknown'));
                const confidence = escapeHtml(String(assessment.confidence || 'medium'));
                const evidence = Array.isArray(assessment.evidence) ? assessment.evidence.slice(0, 3) : [];
                const nextAction = safeString(assessment.next_action || '');
                const summary = safeString(assessment.summary || '');
                const evidenceHtml = evidence.length
                    ? `<ul class="findings-list">${evidence.map(item => `<li class="findings-item">${escapeHtml(String(item))}</li>`).join('')}</ul>`
                    : '';
                const actionButton = nextAction
                    ? `<div class="findings-actions">
                        <button class="findings-action-btn" data-prompt="${encodeURIComponent(nextAction)}" onclick="submitSuggestedPrompt(this)">Run next action</button>
                    </div>`
                    : '';
                return `<div class="findings-panel primary-concern-panel">
                    <div class="findings-label">Correlated assessment</div>
                    <div class="findings-item-title">${escapeHtml(String(assessment.title))}</div>
                    <div class="status-card-meta" style="margin: 10px 0 8px 0;">
                        <span class="status-chip">Severity: ${severity}</span>
                        <span class="status-chip">Confidence: ${confidence}</span>
                    </div>
                    ${summary ? `<div class="findings-item-copy" style="margin-bottom: 10px;">${escapeHtml(summary)}</div>` : ''}
                    ${evidenceHtml}
                    ${nextAction ? `<div class="findings-item-copy" style="margin-top: 10px;">Next action: ${escapeHtml(nextAction)}</div>` : ''}
                    ${actionButton}
                </div>`;
            };

            const renderAssessmentControls = () => {
                if (!runtimeScope && !assessmentMode && !complianceNote && !routeSource) return '';
                return `<div class="findings-panel">
                    <div class="findings-label">Assessment controls</div>
                    <div class="status-card-meta" style="margin: 10px 0 8px 0;">
                        ${routeSource ? `<span class="status-chip">Route: ${escapeHtml(routeSource)}</span>` : ''}
                        ${runtimeScope ? `<span class="status-chip">Scope: ${escapeHtml(runtimeScope)}</span>` : ''}
                        ${assessmentMode ? `<span class="status-chip">Mode: ${escapeHtml(assessmentMode)}</span>` : ''}
                    </div>
                    ${complianceNote ? `<div class="findings-item-copy">${escapeHtml(complianceNote)}</div>` : ''}
                </div>`;
            };

            const renderInvestigationPlan = (plan) => {
                if (!plan || !plan.step) return '';
                const step = safeString(plan.step || '');
                const rationale = safeString(plan.rationale || '');
                const expectedSignal = safeString(plan.expected_signal || '');
                const priority = escapeHtml(String(plan.priority || 'medium'));
                const actionButton = step
                    ? `<div class="findings-actions">
                        <button class="findings-action-btn" data-prompt="${encodeURIComponent(step)}" onclick="submitSuggestedPrompt(this)">Run next diagnostic</button>
                    </div>`
                    : '';
                return `<div class="findings-panel primary-concern-panel">
                    <div class="findings-label">Next diagnostic step</div>
                    <div class="findings-item-title">${escapeHtml(step)}</div>
                    <div class="status-card-meta" style="margin: 10px 0 8px 0;">
                        <span class="status-chip">Priority: ${priority}</span>
                    </div>
                    ${rationale ? `<div class="findings-item-copy">${escapeHtml(rationale)}</div>` : ''}
                    ${expectedSignal ? `<div class="findings-item-subcopy" style="margin-top: 8px;">Expect to confirm: ${escapeHtml(expectedSignal)}</div>` : ''}
                    ${actionButton}
                </div>`;
            };

            const renderRemediationPlan = (plan) => {
                if (!plan || !plan.action) return '';
                const action = safeString(plan.action || '');
                const rationale = safeString(plan.rationale || '');
                const risk = escapeHtml(String(plan.risk || 'medium'));
                const precondition = safeString(plan.precondition || '');
                const rollback = safeString(plan.rollback || '');
                const approvalRequired = plan.approval_required !== false ? 'Approval required' : 'Approval not required';
                const actionButton = action
                    ? `<div class="findings-actions">
                        <button class="findings-action-btn" data-prompt="${encodeURIComponent(action)}" onclick="submitSuggestedPrompt(this)">Run through AVA</button>
                    </div>`
                    : '';
                return `<div class="findings-panel primary-concern-panel">
                    <div class="findings-label">Safest remediation path</div>
                    <div class="findings-item-title">${escapeHtml(action)}</div>
                    <div class="status-card-meta" style="margin: 10px 0 8px 0;">
                        <span class="status-chip">Risk: ${risk}</span>
                        <span class="status-chip">${approvalRequired}</span>
                    </div>
                    ${rationale ? `<div class="findings-item-copy">${escapeHtml(rationale)}</div>` : ''}
                    ${precondition ? `<div class="findings-item-subcopy" style="margin-top: 8px;">Precondition: ${escapeHtml(precondition)}</div>` : ''}
                    ${rollback ? `<div class="findings-item-subcopy" style="margin-top: 8px;">Rollback: ${escapeHtml(rollback)}</div>` : ''}
                    ${actionButton}
                </div>`;
            };

            const renderCandidateList = (title, items) => {
                if (!items.length) return '';
                const rows = items.map(item => {
                    const label = escapeHtml(String(item.prompt || item.package || item.action || 'Remediation candidate'));
                    const detail = item.package && item.action
                        ? `${escapeHtml(String(item.package))} · ${escapeHtml(String(item.action))}`
                        : '';
                    const subcopy = item.command_intent
                        ? `Intent: ${escapeHtml(String(item.command_intent))}`
                        : '';
                    const prompt = safeString(item.prompt || '');
                    const actionButton = prompt
                        ? `<div class="findings-actions">
                            <button class="findings-action-btn" data-prompt="${encodeURIComponent(prompt)}" onclick="submitSuggestedPrompt(this)">Run through AVA</button>
                        </div>`
                        : '';
                    return `<li class="findings-item">
                        <div class="findings-item-title">${label}</div>
                        ${detail ? `<div class="findings-item-copy">${detail}</div>` : ''}
                        ${subcopy ? `<div class="findings-item-subcopy">${subcopy}</div>` : ''}
                        ${actionButton}
                    </li>`;
                }).join('');
                return `<div class="findings-panel">
                    <div class="findings-label">${escapeHtml(title)}</div>
                    <ul class="findings-list">${rows}</ul>
                </div>`;
            };

            const renderMetadataPanels = () => {
                const panels = [
                    renderCorrelatedAssessment(correlatedAssessment),
                    renderPrimaryConcern(primaryConcern),
                    renderInvestigationPlan(investigationPlan),
                    renderRemediationPlan(remediationPlan),
                    renderAssessmentControls(),
                    renderStringList('Alerts', alerts),
                    renderStringList('Suggested actions', suggestedActions),
                    renderCandidateList('Remediation candidates', remediationCandidates),
                    renderStringList('New listeners', newListeners),
                    renderStringList('New failed services', newFailedServices),
                    authFailureDelta > 0 ? renderStringList('Auth trend', [`Authentication failure count increased by ${authFailureDelta} since the previous baseline`]) : ''
                ].filter(Boolean);
                if (!panels.length) return '';
                return `<div class="findings-block">${panels.join('')}</div>`;
            };

            if (normalized.blocked) {
                return `<div class="status-card blocked">
                    <div class="status-card-title">Command blocked</div>
                    <div class="status-card-copy">${reason}</div>
                    <div class="status-card-meta">
                        <span class="status-chip">Risk: ${risk}</span>
                    </div>
                </div>`;
            }

            if (normalized.approval_required) {
                return `<div class="status-card approval">
                    <div class="status-card-title">Approval required</div>
                    <div class="status-card-copy">${reason}</div>
                    <div class="status-card-meta">
                        <span class="status-chip">Risk: ${risk}</span>
                        ${approvalId ? `<span class="status-chip">Approval ID: ${approvalId}</span>` : ''}
                    </div>
                    <div class="terminal-block">
                        <div class="terminal-label">Requested action</div>
                        <pre class="terminal-output"><code>${command}</code></pre>
                    </div>
                </div>`;
            }

            if (normalized.success) {
                const analysisHtml = normalized.analysis ? `<div class="status-card-meta"><span class="status-chip">Analysis available</span></div>` : '';
                const metadataHtml = renderMetadataPanels();
                return `<div class="status-card executed">
                    <div class="status-card-title">Command executed</div>
                    <div class="terminal-block">
                        <div class="terminal-label">Execution output</div>
                        <pre class="terminal-output"><code>${output}</code></pre>
                    </div>
                    ${metadataHtml}
                    ${analysisHtml}
                </div>`;
            }

            return `<div class="status-card blocked">
                <div class="status-card-title">Execution failed</div>
                <div class="status-card-copy">${reason}</div>
                <div class="status-card-meta">
                    <span class="status-chip">Risk: ${risk}</span>
                </div>
            </div>`;
        }
        
        function addAVAMessage(data) {
            removeLoadingMessage();
            const chatArea = document.getElementById('chatArea');
            const messageDiv = document.createElement('div');
            messageDiv.className = 'message';
            data = normalizeChatData(data);
            
            let content = '';
            
            // Handle multi-part responses
            if (data.type === 'multi' && Array.isArray(data.results)) {
                content = '<div style="margin: 8px 0;">';
                content += `<div style="font-size: 13px; color: #667eea; margin-bottom: 12px; font-weight: 600;">📋 Processing ${data.results.length} questions...</div>`;
                
                data.results.forEach((part, idx) => {
                    content += `<div style="margin-bottom: ${idx < data.results.length - 1 ? '24px' : '0'}; padding-bottom: ${idx < data.results.length - 1 ? '24px' : '0'}; border-bottom: ${idx < data.results.length - 1 ? '1px solid #2a2a2a' : 'none'};">`;
                    content += `<div style="font-size: 13px; color: #888; margin-bottom: 8px;">Question ${part.number}: ${escapeHtml(part.question)}</div>`;
                    
                    if (part.type === 'command') {
                        content += renderCommandCard(part.result);
                    } else {
                        content += formatResponse(part.response);
                    }
                    
                    content += '</div>';
                });
                
                content += '</div>';
            }
            // Handle single command response
            else if (data.type === 'command' && data.result) {
                content = renderCommandCard(data.result);
            } else {
                // Handle single knowledge response
                content = formatResponse(data.response);
            }
            
            messageDiv.innerHTML = `
                <div class="message-avatar" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);">AVA</div>
                <div class="message-content">
                    <div class="message-header">
                        <span class="message-author">AVA</span>
                        <span class="message-meta">${data.sources_used ? data.sources_used + ' sources • ' : ''}${data.time_taken || ''}</span>
                    </div>
                    <div class="message-text">${content}</div>
                    <div class="message-actions">
                        <button class="action-btn" onclick="copyMessage(this)">📋 Copy</button>
                    </div>
                </div>
            `;
            chatArea.appendChild(messageDiv); setTimeout(renderMermaidPlaceholders, 100); setTimeout(renderMermaidPlaceholders, 500);
            chatArea.scrollTop = chatArea.scrollHeight;
        }
        
        function sendQuery() {
            const input = document.getElementById('queryInput');
            const sendBtn = document.getElementById('sendBtn');
            const query = input.value.trim();
            if (!query) return;
            
            // Disable input and button during processing
            input.disabled = true;
            sendBtn.disabled = true;
            
            addUserMessage(query);
            input.value = '';
            input.style.height = 'auto';
            
            // Detect if it's likely a command
            const queryLower = query.toLowerCase();
            const isCommand = queryLower.startsWith('run ') || 
                            queryLower.startsWith('execute ') || 
                            queryLower.startsWith('shell ') ||
                            queryLower.includes('show me') ||
                            queryLower.startsWith('show ') ||
                            queryLower.startsWith('list ') ||
                            queryLower.startsWith('check ');
            
            addLoadingMessage(isCommand ? 'executing' : 'searching');
            
            fetch('/ask', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({query: query})
            })
            .then(r => r.json())
            .then(data => {
                addAVAMessage(data);
                loadRecentChats();
            })
            .catch(err => {
                removeLoadingMessage();
                addAVAMessage({ type: 'error', response: 'Error: ' + (err && err.message ? err.message : 'Unknown error') });
            })
            .finally(() => {
                // Re-enable input and button
                input.disabled = false;
                sendBtn.disabled = false;
                input.focus();
            });
        }
        
        function handleFileUpload(input) {
            const file = input.files[0];
            if (!file) return;
            
            const queryInput = document.getElementById('queryInput');
            const sendBtn = document.getElementById('sendBtn');
            
            // Disable during processing
            queryInput.disabled = true;
            sendBtn.disabled = true;
            
            hideWelcome();
            addUserMessage(`📄 Analyzing file: ${file.name}`);
            addLoadingMessage('analyzing');
            
            const formData = new FormData();
            formData.append('file', file);
            
            fetch('/upload', {
                method: 'POST',
                body: formData
            })
            .then(r => r.json())
            .then(data => {
                addAVAMessage(data);
                input.value = '';
                loadRecentChats();
            })
            .catch(err => {
                removeLoadingMessage();
                alert('Error: ' + err.message);
            })
            .finally(() => {
                // Re-enable
                queryInput.disabled = false;
                sendBtn.disabled = false;
                queryInput.focus();
            });
        }
        
        function formatResponse(text) {
            text = safeString(text);
            // Handle mermaid code blocks FIRST
            text = text.replace(/```mermaid\n([\s\S]*?)```/g, function(match, diagram) {
                return '<div class="mermaid-placeholder" data-diagram="' + encodeURIComponent(diagram.trim()) + '" style="background:#1a1a2e;padding:16px;border-radius:8px;margin:8px 0;text-align:center;color:#888;">Loading diagram...</div>';
            });
            // Handle plain ``` blocks containing graph syntax
            text = text.replace(/```[^\n]*\n(graph\s+(?:TD|LR|BT|RL)[\s\S]*?)```/g, function(match, diagram) {
                return '<div class="mermaid-placeholder" data-diagram="' + encodeURIComponent(diagram.trim()) + '" style="background:#1a1a2e;padding:16px;border-radius:8px;margin:8px 0;text-align:center;color:#888;">Loading diagram...</div>';
            });
            text = text.replace(/```(\w+)?\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
            text = text.replace(/`([^`]+)`/g, '<code style="background: #2a2a2a; padding: 2px 6px; border-radius: 4px; font-size: 13px;">$1</code>');
            text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
            text = text.replace(/\n/g, '<br>');
            return text;
        }
        function renderMermaidPlaceholders() {
            document.querySelectorAll('.mermaid-placeholder').forEach(function(el) {
                var diagram = decodeURIComponent(el.getAttribute('data-diagram'));

                // Create container with toggle + export buttons
                var wrapper = document.createElement('div');
                wrapper.style.cssText = 'margin:8px 0;';

                var toolbar = document.createElement('div');
                toolbar.style.cssText = 'display:flex;gap:8px;margin-bottom:6px;';
                toolbar.innerHTML = '<button onclick="toggleDiagramView(this)" style="background:#2a2a3e;border:1px solid #444;color:#aaa;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px;">📊 Diagram</button><button onclick="exportSVG(this)" style="background:#2a2a3e;border:1px solid #444;color:#aaa;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px;">⬇ SVG</button><button onclick="copyMermaid(this)" style="background:#2a2a3e;border:1px solid #444;color:#aaa;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px;">📋 Code</button>';

                var diagramDiv = document.createElement('div');
                diagramDiv.className = 'mermaid';
                diagramDiv.textContent = diagram;
                diagramDiv.style.cssText = 'background:#1a1a2e;padding:16px;border-radius:8px;overflow:auto;';
                diagramDiv.setAttribute('data-raw', diagram);

                var codeDiv = document.createElement('pre');
                codeDiv.style.cssText = 'display:none;background:#1a1a2e;padding:16px;border-radius:8px;color:#a8ff78;font-size:12px;overflow:auto;';
                codeDiv.textContent = diagram;

                wrapper.appendChild(toolbar);
                wrapper.appendChild(diagramDiv);
                wrapper.appendChild(codeDiv);
                el.replaceWith(wrapper);

                if (typeof mermaid !== 'undefined') {
                    try { mermaid.init(undefined, [diagramDiv]); }
                    catch(e) { console.error('Mermaid:', e); }
                }
            });
        }

        function toggleDiagramView(btn) {
            var wrapper = btn.closest('div');
            var diagram = wrapper.querySelector('.mermaid');
            var code = wrapper.querySelector('pre');
            if (diagram.style.display === 'none') {
                diagram.style.display = 'block';
                code.style.display = 'none';
                btn.textContent = '📊 Diagram';
            } else {
                diagram.style.display = 'none';
                code.style.display = 'block';
                btn.textContent = '📝 Code';
            }
        }

        function exportSVG(btn) {
            var wrapper = btn.closest('div');
            var svg = wrapper.querySelector('svg');
            if (!svg) { alert('Diagram not rendered yet'); return; }
            var blob = new Blob([svg.outerHTML], {type:'image/svg+xml'});
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'diagram.svg';
            a.click();
        }

        function copyMermaid(btn) {
            var wrapper = btn.closest('div');
            var diagramDiv = wrapper.querySelector('.mermaid');
            var raw = diagramDiv ? diagramDiv.getAttribute('data-raw') : '';
            navigator.clipboard.writeText(raw || '').then(function() {
                btn.textContent = '✅ Copied';
                setTimeout(function() { btn.textContent = '📋 Code'; }, 2000);
            });
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = safeString(text);
            return div.innerHTML;
        }

        function safeString(value) {
            if (value === null || value === undefined) return '';
            if (typeof value === 'string') return value;
            if (typeof value === 'object') {
                try { return JSON.stringify(value, null, 2); }
                catch (e) { return String(value); }
            }
            return String(value);
        }

        function normalizeChatData(data) {
            const normalized = data && typeof data === 'object' ? Object.assign({}, data) : {};
            normalized.type = safeString(normalized.type || 'knowledge');
            normalized.response = safeString(
                normalized.response ?? normalized.message ?? normalized.analysis ?? normalized.error ?? ''
            );
            normalized.time_taken = safeString(normalized.time_taken || '');
            normalized.sources_used = Number(normalized.sources_used || 0);
            if (!normalized.result || typeof normalized.result !== 'object') {
                normalized.result = null;
            }
            if (!Array.isArray(normalized.results)) {
                normalized.results = [];
            }
            return normalized;
        }
        
        function copyMessage(button) {
            const messageText = button.closest('.message-content').querySelector('.message-text').innerText;
            
            const textarea = document.createElement('textarea');
            textarea.value = messageText;
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.select();
            
            try {
                document.execCommand('copy');
                button.textContent = '✓ Copied';
                setTimeout(() => button.textContent = '📋 Copy', 2000);
            } catch (err) {
                alert('Copy failed');
            }
            
            document.body.removeChild(textarea);
        }
        
        // Security Dashboard Functions
        function showSecurityModal() {
            closeSidebar();
            document.getElementById('securityModal').style.display = 'block';
            loadSecurityData();
        }
        
        function loadSecurityData() {
            Promise.all([
                fetch('/security/stats').then(r => r.json()),
                fetch('/security/audit?count=10').then(r => r.json()),
                fetch('/security/posture').then(r => r.json())
            ])
            .then(([stats, audit, posture]) => {
                displaySecurityData(stats, audit, posture);
            })
            .catch(err => {
                console.error('Error loading security data:', err);
                document.getElementById('securityContent').innerHTML = 
                    '<p style="color: #ff6b6b;">Error loading security data</p>';
            });
        }
        
        function displaySecurityData(stats, audit, posture) {
            const quickApprovalCount = document.getElementById('quickApprovalCount');
            const quickBlockedCount = document.getElementById('quickBlockedCount');
            if (quickApprovalCount) quickApprovalCount.textContent = stats.pending || 0;
            if (quickBlockedCount) quickBlockedCount.textContent = stats.blocked || 0;

            const badge = document.getElementById('securityBadge');
            if (badge) {
                if ((stats.pending || 0) > 0) {
                    badge.textContent = stats.pending;
                    badge.style.display = 'inline-flex';
                } else {
                    badge.style.display = 'none';
                }
            }

            const content = document.getElementById('securityContent');
            
            let html = '<div style="margin-bottom: 24px;">';
            html += '<h3 style="margin-bottom: 12px; font-size: 14px; color: #888; text-transform: uppercase;">Zero-Trust Posture</h3>';
            html += `<div class="stat-card" style="margin-bottom: 12px;">
                <div class="stat-label">Runtime Mode</div>
                <div class="stat-value" style="font-size: 18px;">${escapeHtml(posture.mode || 'unknown')}</div>
                <div style="color: #aaa; font-size: 12px; line-height: 1.55; margin-top: 8px;">${escapeHtml(posture.summary || '')}</div>
            </div>`;

            if (posture.controls && posture.controls.length > 0) {
                posture.controls.forEach(control => {
                    const status = control.status === 'pass' ? 'success' : 'danger';
                    const label = control.status === 'pass' ? 'PASS' : 'WATCH';
                    html += `<div class="security-stat">
                        <span class="security-stat-label">${escapeHtml(control.name || '')}<br><span style="color:#777;font-size:11px;font-weight:400;">${escapeHtml(control.detail || '')}</span></span>
                        <span class="security-stat-value ${status}">${label}</span>
                    </div>`;
                });
            }

            if (posture.remaining_gaps && posture.remaining_gaps.length > 0) {
                html += '<div class="stat-card" style="margin-top: 12px;">';
                html += '<div class="stat-label">Known Remaining Gaps</div>';
                html += '<ul style="margin: 10px 0 0 18px; padding: 0; color: #bbb; font-size: 12px; line-height: 1.65;">';
                posture.remaining_gaps.forEach(gap => {
                    html += `<li>${escapeHtml(gap)}</li>`;
                });
                html += '</ul></div>';
            }

            html += '<div class="findings-actions" style="margin: 14px 0 24px;">';
            ['verify my system', 'check docker', 'look for suspicious activity', 'scan my system for vulnerabilities'].forEach(prompt => {
                html += `<button class="findings-action-btn" data-prompt="${encodeURIComponent(prompt)}" onclick="submitSuggestedPrompt(this)">${escapeHtml(prompt)}</button>`;
            });
            html += '</div>';
            html += '</div>';

            html += '<div style="margin-bottom: 24px;">';
            html += '<h3 style="margin-bottom: 12px; font-size: 14px; color: #888; text-transform: uppercase;">Last 24 Hours</h3>';
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Total Commands</span>
                <span class="security-stat-value">${stats.total_commands}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Executed</span>
                <span class="security-stat-value success">${stats.executed}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Blocked</span>
                <span class="security-stat-value ${stats.blocked > 0 ? 'danger' : ''}">${stats.blocked}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Pending Approval</span>
                <span class="security-stat-value ${stats.pending > 0 ? 'danger' : ''}">${stats.pending}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">High Risk Commands</span>
                <span class="security-stat-value">${stats.high_risk}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Threats Detected</span>
                <span class="security-stat-value ${stats.threats_detected > 0 ? 'danger' : ''}">${stats.threats_detected}</span>
            </div>`;
            
            html += '</div>';
            
            // Recent audit entries
            html += '<div>';
            html += '<h3 style="margin-bottom: 12px; font-size: 14px; color: #888; text-transform: uppercase;">Recent Activity</h3>';
            
            if (audit.entries && audit.entries.length > 0) {
                audit.entries.reverse().forEach(entry => {
                    const eventClass = entry.event_type === 'blocked' ? 'blocked' : 
                                      entry.event_type === 'executed' ? 'executed' : '';
                    const risk = entry.risk_analysis?.risk || 'unknown';
                    const riskClass = risk === 'high' || risk === 'critical' ? 'high' : 'low';
                    
                    html += `<div class="audit-entry ${eventClass}">
                        <div class="audit-time">${new Date(entry.timestamp).toLocaleString()}</div>
                        <div class="audit-command">${escapeHtml(entry.cmd || 'N/A')}</div>
                        <div>
                            <span class="audit-risk ${riskClass}">${risk.toUpperCase()}</span>
                            <span style="color: #888; font-size: 11px;">${entry.event_type.replace(/_/g, ' ').toUpperCase()}</span>
                        </div>
                    </div>`;
                });
            } else {
                html += '<p style="color: #888; text-align: center;">No recent activity</p>';
            }
            
            html += '</div>';
            
            // CLI instructions
            html += `<div style="margin-top: 24px; padding: 16px; background: #1a1a2e; border-radius: 8px; border-left: 3px solid #667eea;">
                <div style="font-size: 12px; color: #667eea; margin-bottom: 6px; font-weight: 600;">📋 CLI Commands</div>
                <div style="color: #ddd; font-size: 13px; line-height: 1.8;">
                    <code style="background: #0a0a0a; padding: 2px 6px; border-radius: 3px;">python3 -m control.security_review</code> - Review pending<br>
                    <code style="background: #0a0a0a; padding: 2px 6px; border-radius: 3px;">python3 -m control.security_review audit 10</code> - View audit log<br>
                    <code style="background: #0a0a0a; padding: 2px 6px; border-radius: 3px;">python3 -m control.security_review export</code> - Export CSV
                </div>
            </div>`;
            
            content.innerHTML = html;
        }
        
        // Update security badge
        function updateSecurityBadge() {
            fetch('/security/stats')
                .then(r => r.json())
                .then(stats => {
                    const badge = document.getElementById('securityBadge');
                    if (stats.pending > 0) {
                        badge.textContent = stats.pending;
                        badge.style.display = 'inline-block';
                    } else {
                        badge.style.display = 'none';
                    }
                })
                .catch(err => console.error('Error updating badge:', err));
        }
        
        // Initialize security features
        updateSecurityBadge();
        setInterval(updateSecurityBadge, 30000); // Update every 30s
    </script>    </script>
</body>
</html>
'''

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("AVA - DevOps AI Agent v2.1.2")
    logger.info(f"Knowledge Base: {STATS['total_chunks']} chunks")

    # Day 8: Register vulnerability scanner tools
    for _vt in vuln_scanner.get_tool_descriptions():
        tool_registry.register_native(
            name=_vt["name"],
            handler=_vt["handler"],
            description=_vt["description"],
            args=_vt["args"],
            risk_level=_vt["risk_level"],
            requires_approval=_vt["requires_approval"],
            available=_vt["available"],
        )
    _vt_avail = vuln_scanner.check_tools()
    logger.info(f"[VulnScanner] Trivy={_vt_avail['trivy']} Lynis={_vt_avail['lynis']}")
    logger.info(f"Model: {STATS['model']}")
    logger.info("=" * 50)
    

# ═══════════════════════════ SCAN ROUTES (Day 8) ════════════════════

@app.route("/scan/check", methods=["GET"])
@jwt_required()
def route_scan_check():
    return jsonify({"tools": vuln_scanner.check_tools(), "install": {"trivy": "curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin", "lynis": "sudo apt install lynis"}})


@app.route("/scan/trivy", methods=["POST"])
@require_admin
@limiter.limit("5 per minute")
def route_scan_trivy():
    from flask_jwt_extended import get_jwt_identity
    body = request.get_json(silent=True) or {}
    image = body.get("image", "").strip()
    if not image:
        return jsonify({"error": "Missing image field"}), 400
    user = get_jwt_identity()
    result = vuln_scanner.scan_trivy(image)
    if result.get("status") == "success" and result.get("risk_level") in ("critical", "high"):
        try:
            report_tool_execution(tool_name="trivy_scan", tool_args={"image": image}, result=result, triggered_by=user, ip_address=request.remote_addr, duration=0)
        except Exception as _e:
            logger.warning(f"[Scan] Auto-report failed: {_e}")
    return jsonify(result)


@app.route("/scan/lynis", methods=["POST"])
@require_admin
@limiter.limit("2 per minute")
def route_scan_lynis():
    from flask_jwt_extended import get_jwt_identity
    user = get_jwt_identity()
    result = vuln_scanner.scan_lynis()
    if result.get("status") == "success" and result.get("risk_level") in ("critical", "high"):
        try:
            report_tool_execution(tool_name="lynis_audit", tool_args={}, result=result, triggered_by=user, ip_address=request.remote_addr, duration=0)
        except Exception as _e:
            logger.warning(f"[Scan] Auto-report failed: {_e}")
    return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False)
