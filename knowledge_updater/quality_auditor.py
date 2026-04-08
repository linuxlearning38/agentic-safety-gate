"""
AVA Knowledge Updater - Phase 5A Day 4
quality_auditor.py — Cross-collection audit: dedup, quality scoring,
embedding health, collection health report, and Phase 5B gap analysis.

Read-only by default. Pass --purge-exact to remove confirmed exact duplicates.

Usage:
    python3 knowledge_updater/quality_auditor.py
    python3 knowledge_updater/quality_auditor.py --purge-exact
"""

import argparse
import hashlib
import json
import logging
import math
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import requests
from chromadb.config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("ava.quality_auditor")

# ── Config ─────────────────────────────────────────────────────────────────────
CHROMA_PATH  = os.getenv("CHROMA_PATH", "/home/manoj/ava-data/chromadb")
OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL  = "nomic-embed-text"
REPORT_PATH  = Path(__file__).parent.parent / "quality_auditor_report.json"

COLLECTIONS = [
    "devops_policies_v2",
    "devops_blogs_v1",
    "devops_patterns_v1",
    "devops_fixes_v1",
]

# Quality thresholds
MIN_CHUNK_LEN   = 100     # flag shorter
MAX_CHUNK_LEN   = 3000    # flag longer
MIN_REAL_WORDS  = 3       # flag chunks with fewer
NEAR_DUP_THRESH   = 5.0    # squared-L2 ≤ this => near-duplicate (non-unit embeddings)
EMBED_GOOD_COS    = 0.55   # min cosine similarity for "well embedded" nearest neighbour
EMBED_FAR_COS     = 0.35   # below this cosine sim => poorly embedded
QUALITY_PASS    = 70      # quality_score threshold for "usable"
EMBED_SAMPLE_N  = 50      # chunks to sample per collection for embedding check

# Gap analysis topics for Phase 5B
PHASE5B_TOPICS = [
    "webhook architecture event-driven",
    "SQLite schema design relational",
    "multi-turn conversation state management",
    "Slack API integration bot",
    "PagerDuty incident management API",
    "Jira API ticket automation",
    "Terraform template provisioning AWS",
    "self-healing infrastructure automation",
]

CONFIDENCE_HIGH   = 0.75   # cosine similarity > 0.75 → strong match
CONFIDENCE_MEDIUM = 0.50   # cosine similarity > 0.50 → moderate match


# ── Helpers ────────────────────────────────────────────────────────────────────
def _client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False)
    )


class EmbeddingClient:
    def __init__(self):
        self.url   = f"{OLLAMA_HOST}/api/embeddings"
        self.model = EMBED_MODEL

    def embed(self, text: str):
        try:
            r = requests.post(self.url, json={"model": self.model, "prompt": text}, timeout=30)
            r.raise_for_status()
            return r.json()["embedding"]
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")
            return None


def _sq_l2_to_cos(sq_l2: float, norm_a: float = 21.0, norm_b: float = 21.0) -> float:
    """
    Convert ChromaDB squared-L2 distance back to cosine similarity.

    ChromaDB "l2" metric stores ||a-b||² (squared Euclidean).
    For any two vectors: ||a-b||² = ||a||² + ||b||² - 2*dot(a,b)
    => cos_sim = (||a||² + ||b||² - sq_l2) / (2 * ||a|| * ||b||)

    nomic-embed-text via Ollama returns un-normalised vectors, norm ≈ 21.
    We calibrate using EMBED_NORM probed at startup.
    """
    denom = 2.0 * norm_a * norm_b
    if denom == 0:
        return 0.0
    return max(0.0, min(1.0, (norm_a ** 2 + norm_b ** 2 - sq_l2) / denom))


# Probed once at startup from a real stored embedding
_EMBED_NORM: float = 21.0


def _probe_embed_norm(client: chromadb.PersistentClient) -> float:
    """Sample one stored embedding to get the actual L2 norm."""
    for name in COLLECTIONS:
        try:
            col  = client.get_collection(name)
            # peek() returns up to 10 items including embeddings
            data = col.peek()
            embs = data.get("embeddings") or []
            if embs and embs[0]:
                emb  = embs[0]
                norm = math.sqrt(sum(x * x for x in emb))
                if norm > 0:
                    logger.info(f"  Embedding norm probed from '{name}': {norm:.2f}")
                    return norm
        except Exception:
            pass
    logger.warning("  Could not probe embedding norm — using default 21.0")
    return 21.0


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


# ── Quality Scorer ─────────────────────────────────────────────────────────────
TECH_WORDS = {
    "kubernetes", "docker", "terraform", "pipeline", "deployment",
    "infrastructure", "container", "monitoring", "observability",
    "prometheus", "grafana", "aws", "azure", "gcp", "devops",
    "ansible", "helm", "jenkins", "gitlab", "github", "redis",
    "postgres", "nginx", "microservice", "service", "cluster",
    "node", "pod", "secret", "config", "deploy", "build", "test",
    "error", "fix", "pattern", "policy", "security", "network",
    "database", "storage", "backup", "restore", "alert", "log",
    "metric", "trace", "incident", "slo", "sli", "sre", "rbac",
    "iam", "vpc", "subnet", "firewall", "ssl", "tls", "certificate",
    "vault", "hashicorp", "datadog", "spacelift", "argo", "flux",
}

PAYWALL_SIGNALS = [
    "subscribe to continue", "subscribe to read", "member-only",
    "sign in to read", "create a free account", "unlock this story",
    "this story is for paying subscribers",
]

CODE_ONLY_PATTERN = re.compile(
    r'^[\s\S]*?```[\s\S]+?```[\s\S]*$'
)


def quality_score(text: str) -> tuple[int, list]:
    """
    Return (score 0-100, list_of_flags).
    Flags: too_short, too_long, few_real_words, paywall, noise, pure_code, non_english
    """
    flags  = []
    score  = 100

    stripped = text.strip()
    length   = len(stripped)
    words    = stripped.split()
    nwords   = len(words)

    # Length checks
    if length < MIN_CHUNK_LEN:
        flags.append("too_short")
        score -= 40
    if length > MAX_CHUNK_LEN:
        flags.append("too_long")
        score -= 15

    # Real-word check (non-numeric, non-symbol, len>2)
    real_words = [w for w in words if re.match(r'^[a-zA-Z]{3,}', w)]
    if len(real_words) < MIN_REAL_WORDS:
        flags.append("few_real_words")
        score -= 50

    # Paywall / noise signals
    lower = stripped.lower()
    if any(p in lower for p in PAYWALL_SIGNALS):
        flags.append("paywall")
        score -= 60

    # Pure code block detection (>80% of lines start with code characters)
    lines       = [l.strip() for l in stripped.splitlines() if l.strip()]
    code_lines  = [l for l in lines if l.startswith(("#", "$", "//", "/*", "```", "---", "{", "}", "[", "]"))]
    if lines and len(code_lines) / len(lines) > 0.80 and length > 200:
        flags.append("pure_code")
        score -= 20   # penalise but don't kill — code has value in DevOps

    # Technical keyword density (reward signal)
    word_set = {w.lower().strip(".,;:()[]{}\"'") for w in words}
    tech_hits = len(word_set & TECH_WORDS)
    if tech_hits >= 5:
        score = min(score + 10, 100)
    elif tech_hits == 0 and nwords > 50:
        flags.append("low_tech_density")
        score -= 10

    # Non-English heuristic: if >50% of real words aren't ASCII letters
    ascii_words = [w for w in real_words if all(ord(c) < 128 for c in w)]
    if real_words and len(ascii_words) / len(real_words) < 0.5:
        flags.append("non_english")
        score -= 30

    return max(0, score), flags


# ── 1. Load all chunks ─────────────────────────────────────────────────────────
def load_all_chunks(client: chromadb.PersistentClient) -> dict:
    """
    Returns:
        {
          collection_name: {
            "ids": [...],
            "documents": [...],
            "metadatas": [...],
          }
        }
    """
    result = {}
    for name in COLLECTIONS:
        try:
            col  = client.get_collection(name)
            data = col.get(include=["documents", "metadatas"])
            result[name] = {
                "ids":       data["ids"],
                "documents": data["documents"],
                "metadatas": data["metadatas"],
            }
            logger.info(f"  Loaded {len(data['ids']):>5} chunks from '{name}'")
        except Exception as e:
            logger.warning(f"  Could not load '{name}': {e}")
            result[name] = {"ids": [], "documents": [], "metadatas": []}
    return result


# ── 2. Cross-collection exact dedup ───────────────────────────────────────────
def find_exact_duplicates(chunks: dict) -> list:
    """
    Returns list of duplicate groups:
    [ {"hash": h, "copies": [{"collection": c, "id": id, "text_preview": "..."}]} ]
    """
    hash_map: dict[str, list] = defaultdict(list)

    for col_name, data in chunks.items():
        for doc_id, doc in zip(data["ids"], data["documents"]):
            h = _text_hash(doc)
            hash_map[h].append({
                "collection":    col_name,
                "id":            doc_id,
                "text_preview":  doc[:120].replace("\n", " "),
            })

    duplicates = []
    for h, copies in hash_map.items():
        if len(copies) > 1:
            duplicates.append({"hash": h, "copies": copies})

    logger.info(f"  Exact duplicates found: {len(duplicates)} groups "
                f"({sum(len(d['copies'])-1 for d in duplicates)} extra copies)")
    return duplicates


# ── 3. Quality score all chunks ────────────────────────────────────────────────
def score_all_chunks(chunks: dict) -> dict:
    """
    Returns:
        {
          collection_name: [
            {"id": id, "score": int, "flags": [...], "length": int}
          ]
        }
    """
    results = {}
    for col_name, data in chunks.items():
        col_results = []
        for doc_id, doc in zip(data["ids"], data["documents"]):
            sc, flags = quality_score(doc)
            col_results.append({
                "id":     doc_id,
                "score":  sc,
                "flags":  flags,
                "length": len(doc),
            })
        results[col_name] = col_results

        low_quality = [r for r in col_results if r["score"] < QUALITY_PASS]
        logger.info(
            f"  '{col_name}': {len(col_results)} chunks, "
            f"{len(low_quality)} below quality threshold ({QUALITY_PASS})"
        )
    return results


# ── 4. Embedding quality check ─────────────────────────────────────────────────
def check_embedding_quality(client: chromadb.PersistentClient, embedder: EmbeddingClient) -> dict:
    """
    Sample EMBED_SAMPLE_N chunks per collection.
    For each, embed and query for top-3 neighbours.
    Flag chunk as 'poorly_embedded' if nearest neighbour distance > EMBED_FAR_THRESH.

    Returns:
        {collection_name: {"sampled": n, "well_embedded": n, "poorly_embedded": n, "pct_good": float}}
    """
    results = {}

    for col_name in COLLECTIONS:
        try:
            col   = client.get_collection(col_name)
            total = col.count()
            if total == 0:
                results[col_name] = {"sampled": 0, "well_embedded": 0, "poorly_embedded": 0, "pct_good": 0.0}
                continue

            # Pull a random sample of docs
            all_data = col.get(include=["documents"])
            all_ids  = all_data["ids"]
            all_docs = all_data["documents"]

            n_sample = min(EMBED_SAMPLE_N, len(all_ids))
            indices  = random.sample(range(len(all_ids)), n_sample)

            well_embedded   = 0
            poorly_embedded = 0
            sample_details  = []

            for idx in indices:
                doc = all_docs[idx]
                emb = embedder.embed(doc[:512])   # cap to 512 chars for speed
                if not emb:
                    poorly_embedded += 1
                    continue

                try:
                    # Query top-4 (first result is the chunk itself)
                    qr = col.query(
                        query_embeddings=[emb],
                        n_results=min(4, total),
                        include=["distances"]
                    )
                    distances = qr["distances"][0]
                    # Skip distance=0 (self) if present
                    nn_dists = [d for d in distances if d > 0.001]

                    if nn_dists:
                        nearest   = min(nn_dists)
                        cos_approx = round(_sq_l2_to_cos(nearest, _EMBED_NORM, _EMBED_NORM), 4)
                        is_good    = cos_approx >= EMBED_GOOD_COS
                        if is_good:
                            well_embedded += 1
                        else:
                            poorly_embedded += 1
                        sample_details.append({
                            "id":            all_ids[idx],
                            "nn_sq_l2":      round(nearest, 4),
                            "cos_sim":       cos_approx,
                            "well_embedded": is_good,
                        })
                    else:
                        # Only chunk in collection
                        well_embedded += 1

                except Exception as e:
                    logger.warning(f"    Embedding check query failed for {col_name}: {e}")
                    poorly_embedded += 1

            pct = round(well_embedded / n_sample * 100, 1) if n_sample > 0 else 0.0
            results[col_name] = {
                "sampled":        n_sample,
                "well_embedded":  well_embedded,
                "poorly_embedded": poorly_embedded,
                "pct_good":       pct,
                "sample_details": sample_details[:10],  # store first 10 for report
            }
            logger.info(
                f"  '{col_name}': {n_sample} sampled → "
                f"{well_embedded} well-embedded ({pct}%), "
                f"{poorly_embedded} poorly embedded"
            )

        except Exception as e:
            logger.warning(f"  Embedding check failed for '{col_name}': {e}")
            results[col_name] = {"sampled": 0, "well_embedded": 0, "poorly_embedded": 0, "pct_good": 0.0}

    return results


# ── 5. Collection health report ────────────────────────────────────────────────
def build_health_report(
    chunks:       dict,
    quality:      dict,
    exact_dups:   list,
    embed_health: dict,
) -> dict:
    """Aggregate everything into a per-collection health snapshot."""

    # Map duplicate chunk IDs for quick lookup
    dup_ids: set = set()
    for group in exact_dups:
        # Keep first copy, flag rest
        for copy in group["copies"][1:]:
            dup_ids.add((copy["collection"], copy["id"]))

    report = {}
    total_usable = 0

    for col_name in COLLECTIONS:
        data      = chunks.get(col_name, {"ids": [], "documents": [], "metadatas": []})
        q_results = quality.get(col_name, [])
        em        = embed_health.get(col_name, {})

        total      = len(data["ids"])
        dup_count  = sum(1 for r in q_results if (col_name, r["id"]) in dup_ids)
        low_q      = [r for r in q_results if r["score"] < QUALITY_PASS]
        high_q     = [r for r in q_results if r["score"] >= QUALITY_PASS]
        usable     = len(high_q)
        total_usable += usable

        # Length distribution
        lengths = [r["length"] for r in q_results]
        hist    = {
            "0-100":      sum(1 for l in lengths if l < 100),
            "100-300":    sum(1 for l in lengths if 100 <= l < 300),
            "300-800":    sum(1 for l in lengths if 300 <= l < 800),
            "800-3000":   sum(1 for l in lengths if 800 <= l < 3000),
            "3000+":      sum(1 for l in lengths if l >= 3000),
        }
        avg_len = round(sum(lengths) / len(lengths), 0) if lengths else 0

        # Flag distribution
        all_flags: dict[str, int] = defaultdict(int)
        for r in q_results:
            for f in r["flags"]:
                all_flags[f] += 1

        # Refresh recommendation
        low_q_pct = len(low_q) / total * 100 if total > 0 else 0
        needs_refresh = low_q_pct > 20

        report[col_name] = {
            "total_chunks":      total,
            "dup_candidates":    dup_count,
            "low_quality":       len(low_q),
            "low_quality_pct":   round(low_q_pct, 1),
            "usable_chunks":     usable,
            "usable_pct":        round(usable / total * 100, 1) if total > 0 else 0,
            "avg_length":        avg_len,
            "length_histogram":  hist,
            "flag_counts":       dict(all_flags),
            "embed_pct_good":    em.get("pct_good", 0.0),
            "needs_refresh":     needs_refresh,
        }

    return report, total_usable


# ── 6. Phase 5B gap analysis ───────────────────────────────────────────────────
def gap_analysis(client: chromadb.PersistentClient, embedder: EmbeddingClient) -> list:
    """
    For each Phase 5B topic, query all collections and assess retrieval confidence.
    Returns list of gap dicts.
    """
    # Load collections
    cols = {}
    for name in COLLECTIONS:
        try:
            col = client.get_collection(name)
            if col.count() > 0:
                cols[name] = col
        except Exception:
            pass

    gaps = []
    logger.info(f"  Running gap analysis on {len(PHASE5B_TOPICS)} topics...")

    for topic in PHASE5B_TOPICS:
        emb = embedder.embed(topic)
        if not emb:
            gaps.append({"topic": topic, "confidence": 0.0, "status": "embed_failed"})
            continue

        best_dist   = float("inf")
        best_source = None
        best_preview = ""

        for col_name, col in cols.items():
            try:
                n = min(3, col.count())
                if n == 0:
                    continue
                qr = col.query(
                    query_embeddings=[emb],
                    n_results=n,
                    include=["documents", "distances"]
                )
                d = qr["distances"][0][0]
                if d < best_dist:
                    best_dist    = d
                    best_source  = col_name
                    best_preview = qr["documents"][0][0][:100].replace("\n", " ")
            except Exception:
                pass

        # ChromaDB "l2" metric returns squared-L2 (||a-b||²).
        # nomic-embed-text is un-normalised (norm ≈ 21); use calibrated formula.
        # Query embedding norm probed live below; stored norm from _EMBED_NORM.
        if best_dist == float("inf"):
            confidence = 0.0
        else:
            q_norm     = math.sqrt(sum(x * x for x in emb)) if emb else _EMBED_NORM
            confidence = round(_sq_l2_to_cos(best_dist, q_norm, _EMBED_NORM), 3)

        if confidence >= CONFIDENCE_HIGH:
            status = "high"
        elif confidence >= CONFIDENCE_MEDIUM:
            status = "medium"
        else:
            status = "gap"

        gaps.append({
            "topic":         topic,
            "confidence":    confidence,
            "status":        status,
            "best_source":   best_source,
            "best_preview":  best_preview,
        })

        icon = "✓" if status == "high" else ("~" if status == "medium" else "✗")
        logger.info(f"    {icon} [{confidence:.3f}] {topic[:60]}")

    return gaps


# ── 7. Purge exact duplicates ──────────────────────────────────────────────────
def purge_exact_duplicates(client: chromadb.PersistentClient, exact_dups: list) -> int:
    """
    For each duplicate group, keep first copy, delete rest.
    Returns total number of chunks deleted.
    """
    deleted = 0
    by_col: dict[str, list] = defaultdict(list)

    for group in exact_dups:
        for copy in group["copies"][1:]:    # skip first (keep it)
            by_col[copy["collection"]].append(copy["id"])

    for col_name, ids_to_delete in by_col.items():
        if not ids_to_delete:
            continue
        try:
            col = client.get_collection(col_name)
            # ChromaDB delete in batches of 100
            for i in range(0, len(ids_to_delete), 100):
                batch = ids_to_delete[i:i+100]
                col.delete(ids=batch)
                deleted += len(batch)
            logger.info(f"  Deleted {len(ids_to_delete)} exact duplicates from '{col_name}'")
        except Exception as e:
            logger.error(f"  Failed to delete from '{col_name}': {e}")

    return deleted


# ── Console formatting ─────────────────────────────────────────────────────────
def _bar(n: int, total: int, width: int = 20) -> str:
    filled = int(width * n / total) if total > 0 else 0
    return "█" * filled + "░" * (width - filled)


def print_report(
    health:       dict,
    total_usable: int,
    exact_dups:   list,
    embed_health: dict,
    gaps:         list,
    purged:       int,
    total_before: int,
):
    W = 62
    print("\n" + "=" * W)
    print("  AVA Quality Audit Report — Phase 5A Day 4")
    print("=" * W)
    print(f"  ChromaDB : {CHROMA_PATH}")
    print(f"  Ran at   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * W)

    # ── Collection Health ──────────────────────────────────────────────────────
    print("\n  COLLECTION HEALTH\n")
    print(f"  {'Collection':<28} {'Total':>6} {'Usable':>7} {'LowQ%':>6} {'EmbOK%':>7} {'Refresh?'}")
    print(f"  {'-'*28} {'-'*6} {'-'*7} {'-'*6} {'-'*7} {'-'*8}")
    total_chunks = 0
    for col, h in health.items():
        flag = "YES ⚠" if h["needs_refresh"] else "no"
        print(
            f"  {col:<28} {h['total_chunks']:>6} "
            f"{h['usable_chunks']:>7} "
            f"{h['low_quality_pct']:>5.1f}% "
            f"{h['embed_pct_good']:>6.1f}% "
            f"  {flag}"
        )
        total_chunks += h["total_chunks"]
    print(f"  {'-'*28} {'-'*6} {'-'*7}")
    print(f"  {'TOTAL':<28} {total_chunks:>6} {total_usable:>7}")

    # ── Duplicates ─────────────────────────────────────────────────────────────
    extra_copies = sum(len(d["copies"]) - 1 for d in exact_dups)
    print(f"\n  EXACT DUPLICATES")
    print(f"  Groups : {len(exact_dups)}")
    print(f"  Extra copies : {extra_copies}")
    if purged > 0:
        print(f"  Purged : {purged} chunks")
    elif extra_copies > 0:
        print(f"  Action : run with --purge-exact to remove {extra_copies} extra copies")
    if exact_dups:
        print(f"\n  Sample duplicate groups (first 3):")
        for group in exact_dups[:3]:
            print(f"    hash: {group['hash'][:16]}...")
            for copy in group["copies"]:
                print(f"      [{copy['collection']}] {copy['id']}")
                print(f"        \"{copy['text_preview'][:70]}...\"")

    # ── Quality Breakdown ──────────────────────────────────────────────────────
    print(f"\n  QUALITY BREAKDOWN")
    print(f"  Total chunks  : {total_chunks}")
    print(f"  Usable (≥{QUALITY_PASS}) : {total_usable}  ({round(total_usable/total_chunks*100,1)}%)")
    print(f"\n  Length distribution across all collections:")
    # Aggregate histograms
    agg_hist: dict[str, int] = defaultdict(int)
    for h in health.values():
        for bucket, count in h["length_histogram"].items():
            agg_hist[bucket] += count
    for bucket in ["0-100", "100-300", "300-800", "800-3000", "3000+"]:
        n = agg_hist[bucket]
        print(f"    {bucket:<10} {n:>5}  {_bar(n, total_chunks)}")

    # Top flags
    agg_flags: dict[str, int] = defaultdict(int)
    for h in health.values():
        for f, cnt in h["flag_counts"].items():
            agg_flags[f] += cnt
    if agg_flags:
        print(f"\n  Quality flags:")
        for flag, cnt in sorted(agg_flags.items(), key=lambda x: -x[1]):
            print(f"    {flag:<22} {cnt:>5}")

    # ── Embedding Health ───────────────────────────────────────────────────────
    print(f"\n  EMBEDDING HEALTH (sampled {EMBED_SAMPLE_N} per collection)")
    for col, em in embed_health.items():
        bar = _bar(em.get("well_embedded", 0), em.get("sampled", 1))
        print(f"  {col:<28} {em.get('pct_good', 0):>5.1f}%  {bar}")

    # ── Phase 5B Gap Analysis ──────────────────────────────────────────────────
    print(f"\n  PHASE 5B GAP ANALYSIS")
    print(f"  {'Topic':<45} {'Conf':>5} {'Status'}")
    print(f"  {'-'*45} {'-'*5} {'-'*8}")
    for g in gaps:
        icon = "✓" if g["status"] == "high" else ("~" if g["status"] == "medium" else "✗ GAP")
        print(f"  {g['topic'][:45]:<45} {g['confidence']:>5.3f}  {icon}")

    gap_topics = [g for g in gaps if g["status"] == "gap"]
    if gap_topics:
        print(f"\n  Recommended scraping for gaps ({len(gap_topics)} topics):")
        for g in gap_topics:
            print(f"    • {g['topic']}")

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n  SUMMARY")
    print(f"  Before audit  : {total_before} total chunks")
    after = total_before - purged
    print(f"  After purge   : {after} total chunks (-{purged})")
    print(f"  Usable chunks : {total_usable} ({round(total_usable/after*100,1) if after else 0}%)")
    print(f"  Dup groups    : {len(exact_dups)} ({extra_copies} extra copies)")
    print(f"  Refresh needed: {sum(1 for h in health.values() if h['needs_refresh'])} collections")
    print("\n" + "=" * W + "\n")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AVA Quality Auditor")
    parser.add_argument(
        "--purge-exact",
        action="store_true",
        help="Delete extra copies of exact-duplicate chunks (keeps first copy per group)"
    )
    args = parser.parse_args()

    print("\n" + "=" * 62)
    print("  AVA Phase 5A Day 4 — Quality Auditor")
    print("=" * 62)
    print(f"  CHROMA_PATH : {CHROMA_PATH}")
    print(f"  OLLAMA_HOST : {OLLAMA_HOST}")
    print(f"  Mode        : {'PURGE EXACT DUPS' if args.purge_exact else 'READ-ONLY'}")
    print("=" * 62 + "\n")

    client   = _client()
    embedder = EmbeddingClient()

    # Probe actual embedding norm from stored vectors for accurate cosine calibration
    global _EMBED_NORM
    _EMBED_NORM = _probe_embed_norm(client)
    logger.info(f"  Using embedding norm = {_EMBED_NORM:.2f} for distance→cosine conversion")

    # ── Step 1: Load ───────────────────────────────────────────────────────────
    print("[ 1/6 ] Loading all chunks from 4 collections...")
    chunks      = load_all_chunks(client)
    total_before = sum(len(v["ids"]) for v in chunks.values())
    print(f"        Total loaded: {total_before} chunks\n")

    # ── Step 2: Exact dedup ────────────────────────────────────────────────────
    print("[ 2/6 ] Finding exact duplicates (SHA-256 hash)...")
    exact_dups = find_exact_duplicates(chunks)
    print()

    # ── Step 3: Quality scoring ────────────────────────────────────────────────
    print("[ 3/6 ] Scoring chunk quality...")
    quality = score_all_chunks(chunks)
    print()

    # ── Step 4: Embedding health ───────────────────────────────────────────────
    print(f"[ 4/6 ] Checking embedding health ({EMBED_SAMPLE_N} samples/collection)...")
    embed_health = check_embedding_quality(client, embedder)
    print()

    # ── Step 5: Health report ──────────────────────────────────────────────────
    print("[ 5/6 ] Building health report...")
    health, total_usable = build_health_report(chunks, quality, exact_dups, embed_health)
    print()

    # ── Step 6: Gap analysis ───────────────────────────────────────────────────
    print("[ 6/6 ] Running Phase 5B gap analysis...")
    gaps = gap_analysis(client, embedder)
    print()

    # ── Optional: purge ────────────────────────────────────────────────────────
    purged = 0
    if args.purge_exact and exact_dups:
        print(f"[PURGE] Removing {sum(len(d['copies'])-1 for d in exact_dups)} exact duplicate chunks...")
        purged = purge_exact_duplicates(client, exact_dups)
        # Reload chunk counts for final report
        for col_name in COLLECTIONS:
            try:
                col = client.get_collection(col_name)
                logger.info(f"  '{col_name}' after purge: {col.count()} chunks")
            except Exception:
                pass
        print()

    # ── Console report ─────────────────────────────────────────────────────────
    print_report(health, total_usable, exact_dups, embed_health, gaps, purged, total_before)

    # ── JSON report ────────────────────────────────────────────────────────────
    report_data = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "chroma_path":   CHROMA_PATH,
        "total_chunks_before": total_before,
        "total_chunks_after":  total_before - purged,
        "total_usable":  total_usable,
        "purged":        purged,
        "exact_duplicates": {
            "groups":      len(exact_dups),
            "extra_copies": sum(len(d["copies"]) - 1 for d in exact_dups),
            "detail":      exact_dups[:50],   # cap to 50 groups
        },
        "collection_health": health,
        "embedding_health":  {
            k: {kk: vv for kk, vv in v.items() if kk != "sample_details"}
            for k, v in embed_health.items()
        },
        "quality_scores": {
            col: [
                {"id": r["id"], "score": r["score"], "flags": r["flags"], "length": r["length"]}
                for r in results
                if r["flags"]   # only flagged chunks in report to keep file small
            ]
            for col, results in quality.items()
        },
        "gap_analysis": gaps,
        "recommendations": {
            "purge_exact_dups":  sum(len(d["copies"]) - 1 for d in exact_dups) > 0,
            "refresh_collections": [c for c, h in health.items() if h["needs_refresh"]],
            "scrape_for_gaps":   [g["topic"] for g in gaps if g["status"] == "gap"],
        },
    }

    with open(REPORT_PATH, "w") as f:
        json.dump(report_data, f, indent=2)
    print(f"  Report saved: {REPORT_PATH}\n")


if __name__ == "__main__":
    main()
