#!/usr/bin/env python3
"""
patch_day2.py — AVA Phase 4 Day 2
Applies Command Graph integration to web_agent_v2.1_guardrail.py

Run from the project root:
    python3 patch_day2.py

What it does:
    1. Adds command_graph import (1 line)
    2. Inserts the graph handler block inside /ask route
    3. Creates a .bak backup first
    4. Verifies the patch applied correctly
"""

import sys
import shutil
from pathlib import Path

TARGET = Path("web_agent_v2.1_guardrail.py")
BACKUP = Path("backups/phase3_close_20260401/web_agent_v2.1_guardrail_pre_day2.bak")

# ─── Patch 1: Import ──────────────────────────────────────────────────────────

OLD_IMPORT = "from control.secure_executor import execute_command_secure"

NEW_IMPORT = """\
from control.secure_executor import execute_command_secure
from control.command_graph import match_graph, execute_graph"""

# ─── Patch 2: Graph handler block inside /ask ─────────────────────────────────
# Inserted immediately before the force_knowledge_routing check

OLD_ANCHOR = "        # Phase 3: Force KNOWLEDGE routing for how/why/fix queries"

GRAPH_BLOCK = """\
        # ── Phase 4: Command Graph — deterministic diagnostics ──────────────
        graph_name = match_graph(query)
        if graph_name:
            logger.info(f"[*] Command Graph matched: {graph_name}")
            graph_result = execute_graph(graph_name, query)

            # If a medium-risk step needs approval, pause and tell the user
            if graph_result.paused_at:
                elapsed = time.time() - start_time
                return jsonify({
                    'type':        'knowledge',
                    'response':    (
                        f"⚠️ **Approval Required**\\n\\n"
                        f"I ran the `{graph_name}` diagnostic and reached a step that "
                        f"needs your approval before continuing:\\n\\n"
                        f"**Tool:** `{graph_result.paused_at}`\\n"
                        f"**Approval ID:** `{graph_result.approval_id}`\\n\\n"
                        f"Run this to approve:\\n"
                        f"```bash\\npython3 -m control.security_review\\n```\\n\\n"
                        f"Steps completed so far:\\n{graph_result.summary_for_ui()}"
                    ),
                    'sources_used': 0,
                    'time_taken':  f"{elapsed:.2f}s",
                    'graph_used':  graph_name,
                })

            # Build context from live tool outputs → send to LLM for analysis
            context_blocks = graph_result.to_context_blocks()
            framing = (
                f"The following is LIVE diagnostic output from running the "
                f"'{graph_name}' diagnostic on the user's system. "
                f"Analyse the output and give a specific diagnosis and fix.\\n"
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

            return jsonify({
                'type':        'knowledge',
                'response':    response,
                'sources_used': len(context_blocks),
                'time_taken':  f"{elapsed:.2f}s",
                'graph_used':  graph_name,
                'steps_run':   [
                    {'tool': s['tool'], 'status': s['status']}
                    for s in graph_result.steps_run
                ],
            })
        # ── End Command Graph ───────────────────────────────────────────────

        # Phase 3: Force KNOWLEDGE routing for how/why/fix queries"""

# ─── Apply ────────────────────────────────────────────────────────────────────

def apply():
    if not TARGET.exists():
        print(f"❌  {TARGET} not found — run this from the project root")
        sys.exit(1)

    src = TARGET.read_text(encoding="utf-8")

    # Check patches aren't already applied
    if "from control.command_graph import" in src:
        print("⚠️  Patch 1 already applied (import exists) — skipping")
        p1_needed = False
    else:
        p1_needed = True

    if "Command Graph matched" in src:
        print("⚠️  Patch 2 already applied (graph block exists) — skipping")
        p2_needed = False
    else:
        p2_needed = True

    if not p1_needed and not p2_needed:
        print("✅  Both patches already applied — nothing to do")
        sys.exit(0)

    # Backup first
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TARGET, BACKUP)
    print(f"📦  Backup saved: {BACKUP}")

    patched = src

    # Apply patch 1 — import
    if p1_needed:
        if OLD_IMPORT not in patched:
            print(f"❌  Could not find import anchor. Aborting.")
            sys.exit(1)
        patched = patched.replace(OLD_IMPORT, NEW_IMPORT, 1)
        print("✅  Patch 1 applied: command_graph import added")

    # Apply patch 2 — graph block
    if p2_needed:
        if OLD_ANCHOR not in patched:
            print(f"❌  Could not find anchor: '{OLD_ANCHOR[:60]}'. Aborting.")
            sys.exit(1)
        patched = patched.replace(OLD_ANCHOR, GRAPH_BLOCK, 1)
        print("✅  Patch 2 applied: command graph handler inserted in /ask route")

    TARGET.write_text(patched, encoding="utf-8")

    # Verify
    final = TARGET.read_text(encoding="utf-8")
    checks = [
        ("command_graph import",    "from control.command_graph import match_graph"),
        ("graph handler",           "Command Graph matched"),
        ("approval pause path",     "graph_result.paused_at"),
        ("context blocks",          "to_context_blocks"),
        ("history save",            "command_graph"),
    ]
    print("\n── Verification ─────────────────────────────────────────────")
    all_ok = True
    for label, snippet in checks:
        ok = snippet in final
        all_ok = all_ok and ok
        print(f"  [{'✅' if ok else '❌'}] {label}")

    print()
    if all_ok:
        print("🎉  All patches verified. Restart AVA to activate:")
        print("     fuser -k 5002/tcp && sleep 2 && python3 web_agent_v2.1_guardrail.py")
    else:
        print("❌  Verification failed — restoring backup")
        shutil.copy2(BACKUP, TARGET)
        sys.exit(1)

if __name__ == "__main__":
    apply()
