#!/usr/bin/env python3
"""
patch_day3.py — AVA Phase 4 Day 3
Integrates ReAct loop into web_agent_v2.1_guardrail.py

Run from project root:
    python3 patch_day3.py

What it does:
    1. Adds react_loop import
    2. Inserts ReAct handler in /ask route — after command graph, before LLM routing
    3. Backs up first, verifies after
"""

import sys
import shutil
from pathlib import Path

TARGET = Path("web_agent_v2.1_guardrail.py")
BACKUP = Path("backups/phase3_close_20260401/web_agent_v2.1_guardrail_pre_day3.bak")

# ─── Patch 1: Import ──────────────────────────────────────────────────────────

OLD_IMPORT = "from control.command_graph import match_graph, execute_graph"

NEW_IMPORT = """\
from control.command_graph import match_graph, execute_graph
from control.react_loop import react_loop"""

# ─── Patch 2: ReAct handler ───────────────────────────────────────────────────
# Inserted immediately before the force_knowledge_routing block
# (which now sits after the command graph block from Day 2)

OLD_ANCHOR = "        # Phase 3: Force KNOWLEDGE routing for how/why/fix queries"

REACT_BLOCK = """\
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

            return jsonify({
                'type':       'knowledge',
                'response':   react_result.final_answer,
                'sources_used': react_result.iterations,
                'time_taken': f"{elapsed:.2f}s",
                'react_trace': [
                    {
                        'iteration':    s.iteration,
                        'thought':      s.thought[:200],
                        'action':       s.action,
                        'observation':  (s.observation or '')[:300],
                        'final_answer': bool(s.final_answer),
                    }
                    for s in react_result.steps
                ],
            })
        # ── End ReAct Loop ──────────────────────────────────────────────────

        # Phase 3: Force KNOWLEDGE routing for how/why/fix queries"""


# ─── Apply ────────────────────────────────────────────────────────────────────

def apply():
    if not TARGET.exists():
        print(f"❌  {TARGET} not found — run from project root")
        sys.exit(1)

    src = TARGET.read_text(encoding="utf-8")

    p1_needed = "from control.react_loop import" not in src
    p2_needed = "ReAct loop triggered" not in src

    if not p1_needed and not p2_needed:
        print("✅  Both patches already applied — nothing to do")
        sys.exit(0)

    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TARGET, BACKUP)
    print(f"📦  Backup saved: {BACKUP}")

    patched = src

    if p1_needed:
        if OLD_IMPORT not in patched:
            print("❌  Import anchor not found. Aborting.")
            sys.exit(1)
        patched = patched.replace(OLD_IMPORT, NEW_IMPORT, 1)
        print("✅  Patch 1 applied: react_loop import added")

    if p2_needed:
        if OLD_ANCHOR not in patched:
            print("❌  Route anchor not found. Aborting.")
            sys.exit(1)
        patched = patched.replace(OLD_ANCHOR, REACT_BLOCK, 1)
        print("✅  Patch 2 applied: ReAct handler inserted in /ask route")

    TARGET.write_text(patched, encoding="utf-8")

    final = TARGET.read_text(encoding="utf-8")
    checks = [
        ("react_loop import",      "from control.react_loop import react_loop"),
        ("ReAct trigger check",    "ReAct loop triggered"),
        ("react signals",          "react_signals"),
        ("rag seed",               "rag_context = query_knowledge_base"),
        ("react history save",     "'type':        'react'"),
        ("react trace in response","react_trace"),
    ]
    print("\n── Verification ─────────────────────────────────────────────")
    all_ok = True
    for label, snippet in checks:
        ok = snippet in final
        all_ok = all_ok and ok
        print(f"  [{'✅' if ok else '❌'}] {label}")

    print()
    if all_ok:
        print("🎉  All patches verified. Restart AVA:")
        print("     fuser -k 5002/tcp && sleep 2 && python3 web_agent_v2.1_guardrail.py")
    else:
        print("❌  Verification failed — restoring backup")
        shutil.copy2(BACKUP, TARGET)
        sys.exit(1)

if __name__ == "__main__":
    apply()
