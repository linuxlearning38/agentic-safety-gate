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
from control.secure_executor import execute_command_secure, execute_approved_command
from control.tool_registry import registry as tool_registry
from control.command_graph import match_graph, execute_graph
from control.react_loop import react_loop
from control.input_router import route_query
from control.evidence_selector import (
    select_ava_self_evidence,
    select_architecture_evidence,
    select_comparison_evidence,
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

limiter = Limiter(
    app=app,
    key_func=_rate_limit_key,
    default_limits=["30 per minute"],
    storage_uri="memory://",
    headers_enabled=True,
    swallow_errors=True,
)
logger.info("[RateLimit] Flask-Limiter initialised — default: 30 req/min per user")


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
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ava-webhook-2026")
_warmup_started = False
_warmup_lock = threading.Lock()

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
start_monitor()

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

def analyze_command_output(cmd, output):
    """Generate brief analysis of command output"""
    try:
        # Skip analysis for empty or very short outputs
        if not output or len(output.strip()) < 10:
            return None
        
        # Create a concise prompt for analysis
        prompt = f"""Analyze this command output and provide a brief 2-3 sentence summary of key insights.

Command: {cmd}
Output:
{output[:1000]}

Provide only the summary, no preamble. Focus on important numbers, status, or findings."""
        
        response = ollama.chat(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        
        return response['message']['content'].strip()
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return None

def execute_command(cmd, query=""):
    """Execute command with AgentGuard security controls"""
    result = execute_command_secure(cmd, query)
    
    if result["status"] == "executed":
        return {
            'success': True,
            'blocked': False,
            'output': result["output"]["stdout"],
            'error': result["output"]["stderr"],
            'returncode': result["output"]["returncode"],
            'security': {
                'risk': result.get("risk"),
                'threats': result.get("threats", [])
            }
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
            'reason': result["reason"],
            'command': cmd,
            'security': {
                'risk': result["risk"],
                'blast_radius': result.get("blast_radius"),
                'threats': result.get("threats", [])
            },
            'suggestion': f'⚠️  Run: python3 control/security_review.py (ID: {result["approval_id"]})'
        }
    
    else:
        return {'success': False, 'blocked': False, 'reason': 'Unknown error'}

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
_GENERIC_HALLUCINATION_TERMS = {
    "frontend", "backend", "event sourcing", "microservices architecture",
}

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
    evidence = select_troubleshooting_evidence(route, _retrieve_troubleshooting_chunks(query))
    plan = build_troubleshooting_plan(route, evidence)
    return {
        "response": compose_controlled_response(plan),
        "confidence": plan.confidence,
        "sources_used": len(evidence.evidence_blocks),
        "topic": plan.topic,
    }


def _retrieve_architecture_chunks(query):
    return hybrid_retriever.query(
        query_text=query,
        n_policies=6,
        n_blogs=4,
        blog_min_relevance=0.008,
        format_for_llm=False,
    )


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
    recent_turns = _get_recent_distinct_turns(limit=4)
    evidence = select_follow_up_evidence(route, recent_turns, _topic_from_turn, _response_summary)
    plan = build_follow_up_plan(route, evidence)
    return {
        "response": compose_controlled_response(plan),
        "confidence": plan.confidence,
        "sources_used": len(recent_turns[:2]),
        "topic": plan.topic,
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

def _hallucination_terms(response_text, entities):
    text = _normalize_text(response_text).lower()
    entity_terms = {e.lower() for e in entities}
    problems = []
    for term in _GENERIC_HALLUCINATION_TERMS:
        if term in text and term not in entity_terms:
            problems.append(term)
    return problems

def _repair_architecture_answer(response_text, entities):
    text = _normalize_text(response_text).strip()
    if not text:
        return text
    lines = [line for line in text.splitlines() if line.strip()]
    if entities:
        entity_lower = [e.lower() for e in entities]
        filtered = []
        for line in lines:
            lower = line.lower()
            if any(term in lower for term in _GENERIC_HALLUCINATION_TERMS) and not any(e in lower for e in entity_lower):
                continue
            filtered.append(line)
        lines = filtered or lines
        if not any(any(e in line.lower() for e in entity_lower) for line in lines):
            lines.insert(0, "Components: " + ", ".join(entities))
    return "\n".join(lines)


def _build_grounded_architecture_answer(context_blocks, entities):
    entities = [entity for entity in (entities or []) if entity][:6]
    relevant_lines = []
    for block in context_blocks or []:
        for line in block.splitlines():
            cleaned = _normalize_text(line).strip(" -\t")
            if cleaned:
                relevant_lines.append(cleaned)
    noise_markers = (
        "docker",
    )
    filtered_lines = []
    for line in relevant_lines:
        lower = line.lower().strip()
        if not lower:
            continue
        if _is_noisy_architecture_line(line):
            continue
        if any(marker in lower for marker in noise_markers):
            continue
        if len(lower.split()) < 4:
            continue
        entity_hits = sum(1 for entity in entities if entity.lower() in lower)
        relation_hits = sum(
            1 for term in ("route", "request", "gateway", "stream", "event", "cache", "store", "read", "write", "monitor", "process", "carry", "ingest")
            if term in lower
        )
        if entities and entity_hits == 0:
            continue
        if entity_hits == 0 and relation_hits == 0:
            continue
        filtered_lines.append(line)
    relevant_lines = filtered_lines[:10]
    if not relevant_lines and not entities:
        return None

    entity_facts = {}
    for entity in entities:
        lower_entity = entity.lower()
        for line in relevant_lines:
            if lower_entity in line.lower():
                entity_facts[entity] = line.strip()
                break

    request_terms = ("request", "route", "gateway", "proxy", "client", "api", "entry")
    data_terms = ("data", "event", "stream", "store", "cache", "read", "write", "persist", "kafka", "cassandra")
    request_flow = []
    data_flow = []
    for line in relevant_lines:
        lower = line.lower()
        if any(term in lower for term in request_terms) and line not in request_flow:
            request_flow.append(line)
        if any(term in lower for term in data_terms) and line not in data_flow:
            data_flow.append(line)

    entity_fact_lower = {entity.lower(): fact for entity, fact in entity_facts.items()}
    synthesized_request = []
    synthesized_data = []
    if "zuul" in entity_fact_lower:
        synthesized_request.append("Zuul sits at the edge and routes incoming requests to backend services.")
    if "kafka" in entity_fact_lower:
        synthesized_request.append("After synchronous request handling, services publish domain events into Kafka for downstream asynchronous work.")
        synthesized_data.append("Kafka acts as the durable event backbone between producers and downstream consumers.")
    if "samza" in entity_fact_lower:
        synthesized_data.append("Samza consumes stream data, processes it, and emits derived outputs or aggregates.")
    if "cassandra" in entity_fact_lower:
        synthesized_data.append("Cassandra stores durable distributed state for serving and downstream systems.")
    if "evcache" in entity_fact_lower:
        synthesized_data.append("EVCache serves hot reads to reduce latency in front of durable stores.")
    if "mantis" in entity_fact_lower:
        synthesized_data.append("Mantis processes or observes streaming data outside the direct end-user request path.")

    for line in synthesized_request:
        if line not in request_flow:
            request_flow.append(line)
    for line in synthesized_data:
        if line not in data_flow:
            data_flow.append(line)

    component_lines = []
    for entity in entities:
        if entity.lower() in {"netflix", "ava"} and len(entities) >= 4:
            continue
        fact = entity_facts.get(entity)
        if fact:
            component_lines.append(f"- {entity}: {fact}")
        else:
            component_lines.append(f"- {entity}")

    tech_lines = []
    for entity in entities:
        if entity.lower() in {"netflix", "ava"} and len(entities) >= 4:
            continue
        fact = entity_facts.get(entity)
        if fact:
            tech_lines.append(f"- {entity}: grounded by '{fact}'")
        else:
            tech_lines.append(f"- {entity}")

    why_used = []
    if request_flow:
        why_used.append("- They are used together to route incoming requests through the right entry and service paths.")
    if data_flow:
        why_used.append("- They are used together to move, store, and cache operational or event data with lower latency.")
    if relevant_lines:
        why_used.append("- Grounded context shows these components are connected by explicit request/data relationships, not just listed independently.")

    sections = ["**Components:**"]
    sections.extend(component_lines or ["- No grounded components found."])
    if request_flow:
        sections.append("\n**Request Flow:**")
        sections.extend(f"- {line}" for line in request_flow[:4])
    if data_flow:
        sections.append("\n**Data Flow:**")
        sections.extend(f"- {line}" for line in data_flow[:4])
    sections.append("\n**Key Technologies:**")
    sections.extend(tech_lines or ["- No grounded technologies found."])
    if why_used:
        sections.append("\n**Why They Are Used:**")
        sections.extend(why_used[:3])
    return "\n".join(sections)


def _looks_generic_architecture_answer(response_text):
    text = _normalize_text(response_text).lower()
    generic_markers = [
        "distributed streaming platform",
        "in-memory cache",
        "monitoring and alerting system",
        "nosql database",
        "requests enter through",
        "routes requests to appropriate microservices",
    ]
    return sum(1 for marker in generic_markers if marker in text) >= 2

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


def _looks_like_mermaid_response(response_text):
    text = _normalize_text(response_text).strip()
    return text.startswith("```mermaid") or text.startswith("graph TD") or text.startswith("graph LR")


def _build_grounded_mermaid_diagram(context_blocks, entities):
    if not entities:
        return None

    unique_entities = []
    seen = set()
    for entity in entities:
        lower = entity.lower()
        if lower not in seen:
            seen.add(lower)
            unique_entities.append(entity)
    unique_entities = unique_entities[:6]
    if not unique_entities:
        return None

    node_ids = {entity.lower(): f"N{idx}" for idx, entity in enumerate(unique_entities, start=1)}
    lines = ["```mermaid", "graph TD"]
    for entity in unique_entities:
        safe_label = entity.replace('"', "'")
        lines.append(f'    {node_ids[entity.lower()]}["{safe_label}"]')

    self_architecture_terms = {entity.lower() for entity in unique_entities}
    if "ava-agent" in self_architecture_terms:
        preferred_edges = [
            ("ava-agent", "flask/gunicorn", "serves"),
            ("ava-agent", "postgresql", "reads/writes"),
            ("ava-agent", "redis", "caches"),
            ("ava-agent", "open policy agent", "checks policy with"),
            ("ava-agent", "hashicorp vault", "uses secrets from"),
            ("ava-agent", "ollama host", "calls"),
        ]
        added_self_edges = False
        for left, right, relation in preferred_edges:
            if left in node_ids and right in node_ids:
                added_self_edges = True
                lines.append(
                    f'    {node_ids[left]} -- "{relation}" --> {node_ids[right]}'
                )
        if added_self_edges:
            lines.append("```")
            return "\n".join(lines)

    relation_terms = [
        "handles", "routes", "calls", "uses", "writes", "stores",
        "publishes", "sends", "reads", "connects", "proxies",
        "feeds", "triggers", "loads", "carries",
    ]
    added_edges = set()
    relevant_lines = _extract_relevant_context_lines(context_blocks, unique_entities, limit=12)
    for line in relevant_lines:
        lower = line.lower()
        present = [entity for entity in unique_entities if entity.lower() in lower]
        if len(present) < 2:
            continue
        left, right = present[0], present[1]
        relation = "flows to"
        for term in relation_terms:
            if term in lower:
                relation = term
                break
        edge_key = (left.lower(), right.lower(), relation)
        if edge_key in added_edges:
            continue
        added_edges.add(edge_key)
        lines.append(
            f'    {node_ids[left.lower()]} -- "{relation}" --> {node_ids[right.lower()]}'
        )

    if not added_edges and len(unique_entities) >= 2:
        for left, right in zip(unique_entities, unique_entities[1:]):
            lines.append(
                f'    {node_ids[left.lower()]} -- "flows to" --> {node_ids[right.lower()]}'
            )

    lines.append("```")
    return "\n".join(lines)


def _repair_diagram_response(response_text, context_blocks, entities):
    text = _normalize_text(response_text).strip()
    if not text:
        return _build_grounded_mermaid_diagram(context_blocks, entities) or text
    if text.startswith("```mermaid"):
        return text
    if text.startswith("graph TD") or text.startswith("graph LR"):
        return f"```mermaid\n{text}\n```"
    grounded = _build_grounded_mermaid_diagram(context_blocks, entities)
    if grounded:
        return grounded + "\n\nGrounded from detected components only."
    return text


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


def _diagram_needs_grounded_override(response_text, entities):
    text = _normalize_text(response_text).lower()
    if not text:
        return True
    generic_markers = [
        "docker host", "docker daemon", "containers:", "each container runs a specific service",
    ]
    if any(marker in text for marker in generic_markers):
        return True
    required_entities = [entity.lower() for entity in (entities or []) if entity]
    if required_entities:
        hits = sum(1 for entity in required_entities if entity in text)
        if hits < min(3, len(required_entities)):
            return True
    return False


def _ava_runtime_diagram_entities():
    return [
        "ava-agent",
        "Flask/Gunicorn",
        "PostgreSQL",
        "Redis",
        "Open Policy Agent",
        "HashiCorp Vault",
        "Ollama Host",
    ]


def _build_ava_runtime_diagram_response():
    return (
        "```mermaid\n"
        "graph LR\n"
        "    A[\"ava-agent:5443\"]\n"
        "    B[\"Flask/Gunicorn\"]\n"
        "    C[\"PostgreSQL:5432\"]\n"
        "    D[\"Redis:6379\"]\n"
        "    E[\"Open Policy Agent:8181\"]\n"
        "    F[\"HashiCorp Vault:8200\"]\n"
        "    G[\"Ollama Host:11434\"]\n"
        "    A -- \"serves via\" --> B\n"
        "    A -- \"reads/writes\" --> C\n"
        "    A -- \"caches with\" --> D\n"
        "    A -- \"checks policy with\" --> E\n"
        "    A -- \"uses secrets from\" --> F\n"
        "    A -- \"calls\" --> G\n"
        "```\n\n"
        "Explanation:\n"
        "- AVA runs as `ava-agent` on port `5443` and uses Flask/Gunicorn to serve requests.\n"
        "- It stores relational data in PostgreSQL, uses Redis for fast state/caching, checks policy with OPA, reads secrets from Vault, and calls the Ollama host for local model inference."
    )

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


def _filter_architecture_chunks(raw_chunks, entities):
    if not raw_chunks or not entities:
        return raw_chunks
    relation_terms = {
        "handles", "routes", "calls", "uses", "writes", "stores", "publishes",
        "sends", "reads", "connects", "proxies", "feeds", "triggers",
        "loads", "carries", "behind", "through", "via", "gateway",
        "stream", "event", "cache", "monitor", "process",
    }
    filtered = []
    for chunk in raw_chunks:
        content = _normalize_text(getattr(chunk, "content", ""))
        lower = content.lower()
        entity_hits = sum(1 for entity in entities if entity.lower() in lower)
        relation_hits = sum(1 for term in relation_terms if term in lower)
        if entity_hits >= 2 or (entity_hits >= 1 and relation_hits >= 1):
            filtered.append(chunk)
    return filtered or raw_chunks

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

def _build_follow_up_response(query):
    recent = _get_recent_distinct_turns(limit=4)
    if not recent:
        return "I don't have enough recent conversation context to answer that follow-up reliably."

    last_turn = recent[-1]
    previous_turn = recent[-2] if len(recent) >= 2 else None
    last_query = _normalize_text(last_turn.get("query"))
    last_response = _normalize_text(last_turn.get("response"))
    last_topic = _topic_from_turn(last_turn)
    last_answer_summary = _response_summary(last_response) or last_response

    q = query.lower().strip()
    if "previous thing" in q or "previous question" in q:
        if previous_turn:
            previous_query = _normalize_text(previous_turn.get("query"))
            previous_topic = _topic_from_turn(previous_turn)
            if previous_topic == last_topic:
                return (
                    f"Your recent questions stayed on the same topic: {last_topic}.\n\n"
                    f"The latest answer summary was: {last_answer_summary}"
                )
            return (
                f"Your most recent topic was {last_topic}.\n\n"
                f"The topic before that was {previous_topic}.\n\n"
                f"They differ because the latest turn focused on {last_topic}, while the earlier turn focused on {previous_topic}.\n\n"
                f"Latest answer summary: {last_answer_summary}"
            )
        return (
            f"Your most recent previous question was about {last_topic}.\n\n"
            f"Latest answer summary: {last_answer_summary}"
        )

    return (
        f"Your most recent previous question was about {last_topic}.\n\n"
        f"Latest answer summary: {last_answer_summary}"
    )

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

def _is_follow_up_query(query):
    q = query.lower().strip()
    return any(phrase in q for phrase in [
        "previous thing", "previous question", "previous thing i asked",
        "previous thing i said", "what did i just say", "what did i just ask",
    ])

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
    if controlled_route.intent in ("ava_self", "memory_store", "memory_recall", "troubleshooting", "architecture", "follow_up", "comparison"):
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
    # Definition guard runs first — "what is/are" always wins regardless of
    # other keywords present in the query (e.g. "what is the fix for...")
    if q.startswith("what is") or q.startswith("what are"):
        return "definition"
    if any(q.startswith(prefix) for prefix in ["define", "explain "]):
        return "definition"
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


_CONFIDENCE_PREFIXES = {
    'medium': "Based on related documentation, ",
    'low': "I don't have a strong match for this, but based on general DevOps knowledge: ",
}

def _answer_known_incident_query(query):
    q = query.lower()
    if "oomkilled" in q or "oom killed" in q:
        return (
            "**Root Cause:** Kubernetes marks a container as `OOMKilled` when it exceeds its memory limit and the kernel terminates it to protect node stability.\n"
            "**Fix:** Raise the pod memory limit if it is too low, reduce the application's memory use, and compare actual peak usage against the current request and limit.\n"
            "**Why this works:** `OOMKilled` is specifically a memory-pressure termination, so the durable fix is aligning the workload's memory behavior with Kubernetes limits.\n"
            "**Watch out for:** A restart can hide the symptom temporarily. Check for memory leaks, bursty traffic, large in-memory caches, or JVM heap settings before only increasing limits."
        )
    if "crashloopbackoff" in q:
        return (
            "**Root Cause:** `CrashLoopBackOff` means the container keeps starting, failing, and being restarted, so Kubernetes backs off between restart attempts.\n"
            "**Fix:** Check the container logs, last termination reason, image entrypoint, env vars, config mounts, and readiness or liveness probe settings. Common causes are bad startup commands, missing config, and application crashes.\n"
            "**Why this works:** `CrashLoopBackOff` is a restart pattern, not the root problem itself. The real fix comes from the failing process or probe.\n"
            "**Watch out for:** If probes are too aggressive, Kubernetes can restart an otherwise healthy app before it finishes booting."
        )
    return None

def _answer_ava_self_query(query, about=None):
    return _resolve_ava_self_response(query, about=about)


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

        if query_intent == "definition":
            system_parts.append("\nFor definition questions: start with a plain-English definition in the first sentence. Then give 2-4 practical details. If the retrieved context is related but incomplete, combine it with standard DevOps knowledge instead of refusing to answer.")

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
        return f"Error generating response: {str(e)}"

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
    Body: {"username": "admin", "password": "<YOUR_ADMIN_PASSWORD>"}
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

def analyze_query_with_llm(query):
    """Use Qwen to analyze query and determine the best action"""
    try:
        # Create a focused prompt for Qwen
        prompt = f"""You are a query analyzer for a DevOps AI assistant. Analyze this query and determine the appropriate action.

Query: "{query}"

Respond in JSON format with ONE of these action types:

1. COMMAND - If user wants to execute a system command
   {{"action": "COMMAND", "command": "exact command to run", "reasoning": "brief explanation"}}

2. DIRECT_ANSWER - If you can answer directly without searching
   {{"action": "DIRECT_ANSWER", "answer": "your concise answer", "reasoning": "brief explanation"}}

3. KNOWLEDGE - If you need to search the knowledge base
   {{"action": "KNOWLEDGE", "reasoning": "brief explanation"}}

Available commands (whitelist):
- Basic: date, whoami, pwd, ls, cat, grep, df, free, ps, top, uptime, uname, echo, head, tail, wc, find, which, hostname
- Server: ollama, docker, systemctl, git, curl, wget, netstat, ss

Examples:
- "check my git status" → {{"action": "COMMAND", "command": "git status", "reasoning": "user wants git repository status"}}
- "what are running processes" → {{"action": "COMMAND", "command": "ps aux", "reasoning": "user wants to see active processes"}}
- "which model are you using" → {{"action": "DIRECT_ANSWER", "answer": "I'm using Qwen 2.5 14B via Ollama", "reasoning": "meta question about the assistant"}}
- "how to secure S3" → {{"action": "KNOWLEDGE", "reasoning": "requires DevOps knowledge base"}}

Respond ONLY with valid JSON, no other text."""

        # Call Qwen via Ollama
        response = ollama.chat(
            model=LLM_MODEL,
            messages=[{'role': 'user', 'content': prompt}],
            options={'temperature': 0.1}  # Low temperature for consistent structured output
        )
        
        result_text = response['message']['content'].strip()
        
        # Try to extract JSON if wrapped in markdown
        import re
        import json
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            result_text = json_match.group(0)
        
        # Parse JSON response
        result = json.loads(result_text)
        
        logger.info(f"[LLM Analysis] Action: {result.get('action')}, Reasoning: {result.get('reasoning')}")
        
        return result
        
    except Exception as e:
        logger.error(f"[LLM Analysis] Error: {e}")
        # Fallback to knowledge search on error
        return {"action": "KNOWLEDGE", "reasoning": "fallback due to analysis error"}

def extract_command_from_query(query):
    """Extract command from natural language query using LLM analysis"""
    analysis = analyze_query_with_llm(query)
    
    if analysis.get('action') == 'COMMAND':
        proposed_cmd = analysis.get('command', '').strip()
        
        if proposed_cmd:
            # Security validation - same whitelist enforcement as before
            is_safe, reason = is_command_safe(proposed_cmd)
            if is_safe:
                logger.info(f"[Command Extraction] Approved: {proposed_cmd}")
                return proposed_cmd
            else:
                logger.warning(f"[Command Extraction] Blocked: {proposed_cmd} - {reason}")
                return None
    
    return None

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
        data = request.json
        query = _normalize_user_query(data.get('query', ''))

        if not query:
            return jsonify({'error': 'No query provided'}), 400

        # FIX 1: Dependency health gate — abort early if LLM is down
        deps = _check_dependencies()
        if not deps["ollama"]:
            logger.warning("[/ask] Ollama unavailable — returning 503")
            return jsonify({
                "error":        "ollama_unavailable",
                "confidence":   "low",
                "response":     "LLM service is unavailable. Cannot process query.",
                "dependencies": deps,
            }), 503

        logger.info(f"Query: {query}")
        controlled_route = _route_query(query)

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

        if controlled_route.intent == "troubleshooting":
            resolved = _resolve_troubleshooting_response(query)
            response = resolved["response"]
            elapsed = time.time() - start_time
            _record_query(query, response, "troubleshooting", elapsed, sources_used=resolved["sources_used"], confidence=resolved["confidence"])
            return jsonify(_chat_payload(
                response,
                response_type="knowledge",
                confidence=resolved["confidence"],
                sources_used=resolved["sources_used"],
                time_taken=f"{elapsed:.2f}s",
            ))

        if controlled_route.intent == "architecture":
            resolved = _resolve_architecture_response(query)
            response = resolved["response"]
            elapsed = time.time() - start_time
            response_type = "diagram" if resolved["response_mode"] == "diagram" else "knowledge"
            _record_query(query, response, "architecture", elapsed, sources_used=resolved["sources_used"], confidence=resolved["confidence"])
            return jsonify(_chat_payload(
                response,
                response_type=response_type,
                confidence=resolved["confidence"],
                sources_used=resolved["sources_used"],
                time_taken=f"{elapsed:.2f}s",
            ))

        if controlled_route.intent == "follow_up":
            resolved = _resolve_follow_up_response(query)
            response = resolved["response"]
            elapsed = time.time() - start_time
            _record_query(query, response, "follow_up", elapsed, sources_used=resolved["sources_used"], confidence=resolved["confidence"])
            return jsonify(_chat_payload(
                response,
                response_type="knowledge",
                confidence=resolved["confidence"],
                sources_used=resolved["sources_used"],
                time_taken=f"{elapsed:.2f}s",
            ))

        if controlled_route.intent == "comparison":
            resolved = _resolve_comparison_response(query)
            response = resolved["response"]
            elapsed = time.time() - start_time
            _record_query(query, response, "comparison", elapsed, sources_used=resolved["sources_used"], confidence=resolved["confidence"])
            return jsonify(_chat_payload(
                response,
                response_type="knowledge",
                confidence=resolved["confidence"],
                sources_used=resolved["sources_used"],
                time_taken=f"{elapsed:.2f}s",
            ))

        if _is_healing_query(query) or detect_query_intent(query) == "healing_incident":
            response, healing_meta = _build_healing_response(query)
            elapsed = time.time() - start_time
            _record_query(query, response, "healing_incident", elapsed, confidence="high")
            return jsonify(_chat_payload(
                response,
                response_type="healing",
                confidence="high",
                time_taken=f"{elapsed:.2f}s",
                healing=healing_meta,
                action_taken=healing_meta.get("action_taken"),
            ))
        
        # Handle greetings
        if is_greeting(query):
            response = "Hello! I'm AVA, your local DevOps AI assistant. How can I help you today with infrastructure, containers, or cloud services?"
            elapsed = time.time() - start_time
            
            save_history({
                'timestamp': datetime.now().isoformat(),
                'query': query,
                'type': 'greeting',
                'time_taken': f"{elapsed:.2f}s"
            })
            
            return jsonify(_chat_payload(
                response,
                response_type='knowledge',
                time_taken=f"{elapsed:.2f}s"
            ))

        if controlled_route.intent == "ava_self":
            response = _resolve_ava_self_response(query)
            elapsed = time.time() - start_time
            _record_query(query, response, "ava_self", elapsed, confidence="high")
            return jsonify(_chat_payload(
                response,
                response_type="knowledge",
                confidence="high",
                time_taken=f"{elapsed:.2f}s",
            ))
        
        # Multi-question detection — split and answer each separately
        questions = detect_multiple_questions(query)
        if len(questions) > 1:
            logger.info(f"[*] Multi-question detected: {len(questions)} questions")
            combined_parts = []
            total_sources = 0

            for idx, q in enumerate(questions, 1):
                logger.info(f"[{idx}/{len(questions)}] Processing: {q}")
                q_route = _route_query(q)
                if q_route.intent == "ava_self":
                    q_context = []
                    q_confidence = "high"
                    q_response = _resolve_ava_self_response(q)
                elif q_route.intent == "troubleshooting":
                    q_context = []
                    q_resolved = _resolve_troubleshooting_response(q)
                    q_confidence = q_resolved["confidence"]
                    q_response = q_resolved["response"]
                elif q_route.intent == "architecture":
                    q_context = []
                    q_resolved = _resolve_architecture_response(q)
                    q_confidence = q_resolved["confidence"]
                    q_response = q_resolved["response"]
                elif q_route.intent == "follow_up":
                    q_context = []
                    q_resolved = _resolve_follow_up_response(q)
                    q_confidence = q_resolved["confidence"]
                    q_response = q_resolved["response"]
                elif q_route.intent == "comparison":
                    q_context = []
                    q_resolved = _resolve_comparison_response(q)
                    q_confidence = q_resolved["confidence"]
                    q_response = q_resolved["response"]
                else:
                    q_context = query_knowledge_base(q, query_intent=detect_query_intent(q))
                    q_confidence = score_context_confidence(q_context, q)
                    q_response = generate_response(q, q_context, confidence=q_confidence)
                combined_parts.append(
                    f"---\n\n**Question {idx}:** {q}\n\n{q_response}"
                )
                total_sources += len(q_context)

            combined_response = "\n\n".join(combined_parts)
            elapsed = time.time() - start_time

            save_history({
                'timestamp': datetime.now().isoformat(),
                'query': query,
                'type': 'multi',
                'parts': len(questions),
                'time_taken': f"{elapsed:.2f}s"
            })

            return jsonify(_chat_payload(
                combined_response,
                response_type='knowledge',
                sources_used=total_sources,
                time_taken=f"{elapsed:.2f}s"
            ))
        
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
                return jsonify(_chat_payload(
                    (
                        f"⚠️ **Approval Required**\n\n"
                        f"I ran the `{graph_name}` diagnostic and reached a step that "
                        f"needs your approval before continuing:\n\n"
                        f"**Tool:** `{graph_result.paused_at}`\n"
                        f"**Approval ID:** `{graph_result.approval_id}`\n\n"
                        f"Run this to approve:\n"
                        f"```bash\npython3 -m control.security_review\n```\n\n"
                        f"Steps completed so far:\n{graph_result.summary_for_ui()}"
                    ),
                    response_type='knowledge',
                    sources_used=0,
                    time_taken=f"{elapsed:.2f}s",
                    graph_used=graph_name,
                ))

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
            save_history({
                'timestamp':  datetime.now().isoformat(),
                'query':      query,
                'type':       'command_graph',
                'graph':      graph_name,
                'steps':      len(graph_result.steps_run),
                'time_taken': f"{elapsed:.2f}s",
            })

            return jsonify(_chat_payload(
                response,
                response_type='knowledge',
                sources_used=len(context_blocks),
                time_taken=f"{elapsed:.2f}s",
                graph_used=graph_name,
                steps_run=[
                    {'tool': s['tool'], 'status': s['status']}
                    for s in graph_result.steps_run
                ],
            ))
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
            save_history({
                'timestamp':   datetime.now().isoformat(),
                'query':       query,
                'type':        'react',
                'iterations':  react_result.iterations,
                'stopped':     react_result.stopped_reason,
                'tools_used':  [s.action for s in react_result.steps if s.action],
                'time_taken':  f"{elapsed:.2f}s",
            })

            return jsonify(_chat_payload(
                react_result.final_answer or "I was unable to complete the diagnostic. Please check kubectl is available in this environment.",
                response_type='knowledge',
                sources_used=react_result.iterations,
                time_taken=f"{elapsed:.2f}s",
                react_trace=[
                    {
                        'iteration':    s.iteration,
                        'thought':      s.thought[:200],
                        'action':       s.action,
                        'observation':  (s.observation or '')[:300],
                        'final_answer': bool(s.final_answer),
                    }
                    for s in react_result.steps
                ],
            ))
        # ── End ReAct Loop ──────────────────────────────────────────────────

        # Phase 3: Force KNOWLEDGE routing for how/why/fix queries
        if force_knowledge_routing(query):
            logger.info("[*] Force-routing to knowledge base (knowledge pattern detected)")
            action_type = "KNOWLEDGE"
            analysis = {"action": "KNOWLEDGE", "reasoning": "forced by knowledge pattern"}
        else:
            # Use LLM to analyze query and decide action
            logger.info("[*] Analyzing query with LLM...")
            analysis = analyze_query_with_llm(query)
            action_type = analysis.get('action')
        
        if action_type == 'COMMAND':
            # Try to extract and validate command
            extracted_cmd = extract_command_from_query(query)
            if extracted_cmd:
                logger.info(f"Extracted command: {extracted_cmd}")
                result = execute_command(extracted_cmd, query)
                
                # Add analysis for successful commands
                if result.get('success') and result.get('output'):
                    logger.info("[*] Analyzing command output...")
                    cmd_analysis = analyze_command_output(extracted_cmd, result['output'])
                    if cmd_analysis:
                        result['analysis'] = cmd_analysis
                
                elapsed = time.time() - start_time
                
                save_history({
                    'timestamp': datetime.now().isoformat(),
                    'query': query,
                    'type': 'command',
                    'blocked': result.get('blocked', False),
                    'time_taken': f"{elapsed:.2f}s"
                })
                
                return jsonify(_chat_payload(
                    result.get('output') or result.get('error') or result.get('reason') or "",
                    response_type='command',
                    time_taken=f"{elapsed:.2f}s",
                    result=result,
                ))
            else:
                # Command blocked by security
                elapsed = time.time() - start_time
                return jsonify(_chat_payload(
                    "This command was blocked by security restrictions.",
                    response_type='knowledge',
                    sources_used=0,
                    time_taken=f"{elapsed:.2f}s"
                ))
                
        elif action_type == 'DIRECT_ANSWER':
            # Use LLM's direct answer
            logger.info("[*] Using direct answer from LLM")
            answer = analysis.get('answer', 'I can help with that.')
            elapsed = time.time() - start_time
            
            save_history({
                'timestamp': datetime.now().isoformat(),
                'query': query,
                'type': 'direct_answer',
                'time_taken': f"{elapsed:.2f}s"
            })
            
            return jsonify(_chat_payload(
                answer,
                response_type='knowledge',
                sources_used=0,
                time_taken=f"{elapsed:.2f}s"
            ))
        
        else:  # KNOWLEDGE or fallback
            # Handle regular DevOps questions with RAG
            logger.info("[*] Searching knowledge base...")
            query_intent = detect_query_intent(query)
            context = query_knowledge_base(query, query_intent=query_intent)
            logger.info(f"[*] Found {len(context)} relevant chunks")

            # Phase 4.5: Score context confidence before generating
            confidence = score_context_confidence(context, query)
            confidence = _apply_confidence_rules(confidence, context, query)
            logger.info(f"[*] Context confidence: {confidence}")
            logger.info(f"[*] Thinking with {LLM_MODEL}...")

            # Phase 5B: load recent conversation history for multi-turn context
            prior_messages = _get_recent_prior_messages(n=3)
            if prior_messages:
                logger.info(f"[MultiTurn] Injecting {len(prior_messages) // 2} prior turns")

            response = generate_response(query, context, confidence=confidence, prior_messages=prior_messages)
            if query_intent != "healing_incident" and _looks_like_invalid_json_wrapper(response):
                response = _repair_definition_wrapper(response)

            # Phase 3: Fail-safe retry
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
                    confidence = retry_confidence
                    logger.info("[FAILSAFE] Retry complete")

            # Pending 3: Memory auto-update — persist resolved issues
            if is_technical_query(query) and not is_weak_response(response):
                update_memory_issue(query, response[:200])
                logger.info("[MEMORY] Issue saved to ava_memory.json")

            # Phase 4.5: Update live stats counters
            STATS['query_count'] += 1
            # token count: word count approximation (more accurate than char/4)
            response_tokens = len(response.split())
            STATS['total_tokens'] += response_tokens
            STATS['avg_tokens_per_query'] = STATS['total_tokens'] // STATS['query_count']

            elapsed = time.time() - start_time

            save_history({
                'timestamp': datetime.now().isoformat(),
                'query': query,
                'type': 'knowledge',
                'sources_used': len(context),
                'time_taken': f"{elapsed:.2f}s",
                'response_preview': response[:200] + '...' if len(response) > 200 else response
            })

            # Phase 5B: persist to SQLite for multi-turn retrieval
            # Strip confidence prefix so it doesn't compound on future turns
            try:
                clean_response = response
                for prefix in _CONFIDENCE_PREFIXES.values():
                    if clean_response.startswith(prefix):
                        clean_response = clean_response[len(prefix):]
                        break
                db.save_query(
                    query=query,
                    response=clean_response,
                    confidence=confidence,
                    intent=query_intent,
                    sources_used=len(context),
                )
            except Exception as _dbe:
                logger.warning(f"[DB] save_query failed: {_dbe}")

            return jsonify(_chat_payload(
                response,
                response_type='knowledge',
                sources_used=len(context),
                confidence=confidence,
                time_taken=f"{elapsed:.2f}s"
            ))
        
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
        return jsonify({"status": "error", "message": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


@app.route('/healing/history', methods=['GET'])
@jwt_required()
def healing_history():
    """Phase 5C — Returns last 20 self-healing audit entries."""
    try:
        history = healer.get_healing_history(20)
        return jsonify({"history": history, "total": len(history)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        return jsonify({'error': 'Failed to analyze file', 'details': str(e)}), 500

@app.route('/history', methods=['GET'])
@jwt_required()
def get_history():
    try:
        history = load_history()
        return jsonify({'history': history[-50:], 'total': len(history)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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

        if result.get('status') == 'executed':
            return jsonify({
                'status':  'executed',
                'command': result.get('command', ''),
                'output':  result.get('output', {}),
            })
        else:
            return jsonify({
                'status': 'error',
                'error':  result.get('error', 'Unknown error'),
            }), 400

    except Exception as e:
        logger.error(f"Error in execute_approved: {e}")
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


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

        if tool.risk_level != 'low':
            return jsonify({
                'error':      f"Tool '{tool_name}' is {tool.risk_level} risk",
                'message':    'Use /ask to run medium/high risk tools through the approval workflow',
                'risk_level': tool.risk_level,
            }), 403

        logger.info(f"[Tool] Direct run: {tool_name}({tool_args})")
        _t0    = time.time()
        result = tool_registry.execute(tool_name, tool_args)
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
        })

    except Exception as e:
        logger.error(f"Error running tool {tool_name}: {e}")
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


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
        "storage": "in-memory (resets on restart)",
        "headers": "X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset",
        "note":    "Rate limits are per-user for authenticated endpoints, per-IP for login.",
    }
    return jsonify(limits)


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
        return jsonify({'error': str(e)}), 500

@app.route('/security/audit', methods=['GET'])
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
        return jsonify({'error': str(e)}), 500

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
        
        @keyframes slideUp {
            from { transform: translateY(50px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
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
    </style>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <script>mermaid.initialize({ startOnLoad: false, theme: 'dark' });</script>
</head>
<body>

    < Day 5: Login Overlay -->
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
    < End Login Overlay -->

    <div class="app-container">
        <!-- Sidebar -->
        <div class="sidebar">
            <div class="sidebar-header">
                <div class="sidebar-title">AVA</div>
                <div id="userBadge" style="
                    margin-top:8px; padding:6px 10px;
                    background:#1a1a2e; border:1px solid #2a2a4a;
                    border-radius:8px; font-size:12px; color:#888;
                    display:flex; align-items:center; justify-content:space-between;
                ">
                    <span>
                        <span style="color:#667eea;">&#9632;</span>
                        <span id="userBadgeName" style="color:#ccc; margin-left:4px;">...</span>
                        <span id="userBadgeRole" style="
                            margin-left:6px; font-size:10px; padding:2px 6px;
                            border-radius:4px; background:#2a2a4a; color:#888;
                        "></span>
                    </span>
                    <button onclick="logoutAva()" title="Sign out" style="
                        background:none; border:none; color:#555; cursor:pointer;
                        font-size:16px; padding:0 2px; line-height:1;
                    " onmouseover="this.style.color='#ff6b6b'"
                       onmouseout="this.style.color='#555'">&#x23FB;</button>
                </div>
            </div>
            
            <button class="sidebar-btn" onclick="newChat()">
                <span>+</span>
                <span>New chat</span>
            </button>
            
            <button class="sidebar-btn" onclick="showHistoryModal()">
                <span>📜</span>
                <span>History</span>
            </button>
            
            <button class="sidebar-btn" onclick="showStatsModal()">
                <span>📊</span>
                <span>Stats</span>
            </button>
            
            <button class="sidebar-btn" onclick="showSettingsModal()">
                <span>⚙️</span>
                <span>Settings</span>
            </button>
            
            <button class="sidebar-btn" onclick="showSecurityModal()">
                <span>🛡️</span>
                <span>Security</span>
                <span id="securityBadge" class="badge" style="display: none;"></span>
            </button>
            
            <!-- Recent Chats removed from sidebar — use History button instead -->
            <div id="recentChats" style="display:none;"></div>

            < Bottom user badge — like Claude sidebar -->
            <div id="userBadge" onclick="logoutAva()" title="Click to sign out" style="
                position:absolute; bottom:0; left:0; right:0;
                padding:12px 14px;
                background:#0f0f1a;
                border-top:1px solid #2a2a4a;
                display:flex; align-items:center; gap:10px;
                cursor:pointer; transition:background 0.2s;
            "
            onmouseover="this.style.background='#1a1a2e'"
            onmouseout="this.style.background='#0f0f1a'">
                <div style="
                    width:32px; height:32px; border-radius:50%;
                    background:linear-gradient(135deg,#667eea,#764ba2);
                    display:flex; align-items:center; justify-content:center;
                    font-size:14px; font-weight:700; color:white; flex-shrink:0;
                " id="userAvatar">M</div>
                <div style="flex:1; min-width:0;">
                    <div id="userBadgeName" style="
                        color:#e0e0e0; font-size:13px; font-weight:500;
                        white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
                    ">...</div>
                    <div id="userBadgeRole" style="
                        font-size:11px; color:#667eea; margin-top:1px;
                    ">...</div>
                </div>
                <div title="Sign out" style="color:#444; font-size:14px;">&#x23FB;</div>
            </div>
        </div>
        
        <!-- Main Content -->
        <div class="main-content">
            <!-- Top bar removed for more space - stats moved to Stats modal -->
            
            <!-- Chat Area -->
            
            <div class="chat-area" id="chatArea">
                <div class="welcome-screen" id="welcomeScreen">
                    <div class="welcome-title">What can I help with?</div>
                    <div class="welcome-subtitle">Ask about DevOps, infrastructure, Terraform, Kubernetes, or run shell commands</div>
                    
                    <div class="example-prompts">
                        <div class="example-prompt" onclick="askExample('How to secure S3 buckets in production?')">
                            <div class="example-icon">🔒</div>
                            <div class="example-text">Secure S3 buckets</div>
                        </div>
                        <div class="example-prompt" onclick="askExample('Design a highly available RDS setup')">
                            <div class="example-icon">🗄️</div>
                            <div class="example-text">HA database design</div>
                        </div>
                        <div class="example-prompt" onclick="askExample('How to reduce Docker image size?')">
                            <div class="example-icon">🐳</div>
                            <div class="example-text">Optimize Docker images</div>
                        </div>
                        <div class="example-prompt" onclick="askExample('Kubernetes deployment strategies')">
                            <div class="example-icon">☸️</div>
                            <div class="example-text">K8s deployments</div>
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
    <div id="historyModal" class="modal">
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
    <div id="statsModal" class="modal">
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
    <div id="securityModal" class="modal">
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
            if (nameEl) nameEl.textContent = window._avaUser || '';
            const avatarEl = document.getElementById('userAvatar');
            if (avatarEl && window._avaUser) avatarEl.textContent = window._avaUser[0].toUpperCase();
            if (roleEl) {
                roleEl.textContent = role;
                roleEl.style.background = role === 'admin' ? '#1a3a1a' : '#2a2a4a';
                roleEl.style.color = role === 'admin' ? '#4caf50' : '#888';
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
                        `<div class="chat-item">${h.query.substring(0, 30)}${h.query.length > 30 ? '...' : ''}</div>`
                    ).join('');
                })
                .catch(err => {
                    console.error('Error loading recent chats:', err);
                });
        }
        
        function newChat() {
            console.log('newChat called');
            try {
                location.reload();
            } catch (err) {
                console.error('Error in newChat:', err);
            }
        }
        
        function showHistoryModal() {
            console.log('showHistoryModal called');
            try {
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
                document.getElementById('statsModal').style.display = 'block';
                fetch('/stats')
                    .then(r => r.json())
                    .then(data => {
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
                        if (part.result.blocked) {
                            content += `<div style="color: #ff6b6b; padding: 12px; background: #2a1a1a; border-left: 3px solid #ff6b6b; border-radius: 6px;">
                                <strong>🛡️ Command Blocked</strong><br>
                                ${escapeHtml(part.result.reason)}
                            </div>`;
                        } else if (part.result.success) {
                            const output = escapeHtml(part.result.output || 'Command executed successfully');
                            let analysisHtml = '';
                            if (part.result.analysis) {
                                analysisHtml = `<div style="margin-top: 12px; padding: 12px; background: #1a1a2e; border-left: 3px solid #667eea; border-radius: 6px;">
                                    <div style="font-size: 12px; color: #667eea; margin-bottom: 6px; font-weight: 600;">📊 Analysis</div>
                                    <div style="color: #ddd; font-size: 14px; line-height: 1.6;">${escapeHtml(part.result.analysis)}</div>
                                </div>`;
                            }
                            content += `<div style="font-size: 12px; color: #667eea; margin-bottom: 6px;">✓ Command executed</div>
                                <pre style="background: #0a0a0a; border: 1px solid #333; border-radius: 8px; padding: 16px; overflow-x: auto; margin: 0;">
                                    <code style="color: #00ff00; font-family: 'Monaco', 'Courier New', monospace; font-size: 13px; line-height: 1.6;">${output}</code>
                                </pre>
                                ${analysisHtml}`;
                        }
                    } else {
                        content += formatResponse(part.response);
                    }
                    
                    content += '</div>';
                });
                
                content += '</div>';
            }
            // Handle single command response
            else if (data.type === 'command' && data.result) {
                if (data.result.blocked) {
                    content = `<div style="color: #ff6b6b; padding: 12px; background: #2a1a1a; border-left: 3px solid #ff6b6b; border-radius: 6px;">
                        <strong>🛡️ Command Blocked</strong><br>
                        ${escapeHtml(data.result.reason)}<br>
                        <small style="color: #aaa;">${data.result.suggestion || ''}</small>
                    </div>`;
                } else if (data.result.success) {
                    const output = escapeHtml(data.result.output || 'Command executed successfully');
                    let analysisHtml = '';
                    
                    // Add analysis if available
                    if (data.result.analysis) {
                        analysisHtml = `<div style="margin-top: 12px; padding: 12px; background: #1a1a2e; border-left: 3px solid #667eea; border-radius: 6px;">
                            <div style="font-size: 12px; color: #667eea; margin-bottom: 6px; font-weight: 600;">📊 Analysis</div>
                            <div style="color: #ddd; font-size: 14px; line-height: 1.6;">${escapeHtml(data.result.analysis)}</div>
                        </div>`;
                    }
                    
                    content = `<div style="margin: 8px 0;">
                        <div style="font-size: 12px; color: #667eea; margin-bottom: 6px;">✓ Command executed</div>
                        <pre style="background: #0a0a0a; border: 1px solid #333; border-radius: 8px; padding: 16px; overflow-x: auto; margin: 0;">
                            <code style="color: #00ff00; font-family: 'Monaco', 'Courier New', monospace; font-size: 13px; line-height: 1.6;">${output}</code>
                        </pre>
                        ${analysisHtml}
                    </div>`;
                } else {
                    content = `<div style="color: #ff6b6b; padding: 12px; background: #2a1a1a; border-left: 3px solid #ff6b6b; border-radius: 6px;">
                        <strong>❌ Error</strong><br>
                        ${escapeHtml(data.result.reason || 'Command failed')}
                    </div>`;
                }
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
            document.getElementById('securityModal').style.display = 'block';
            loadSecurityData();
        }
        
        function loadSecurityData() {
            Promise.all([
                fetch('/security/stats').then(r => r.json()),
                fetch('/security/audit?count=10').then(r => r.json())
            ])
            .then(([stats, audit]) => {
                displaySecurityData(stats, audit);
            })
            .catch(err => {
                console.error('Error loading security data:', err);
                document.getElementById('securityContent').innerHTML = 
                    '<p style="color: #ff6b6b;">Error loading security data</p>';
            });
        }
        
        function displaySecurityData(stats, audit) {
            const content = document.getElementById('securityContent');
            
            let html = '<div style="margin-bottom: 24px;">';
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
