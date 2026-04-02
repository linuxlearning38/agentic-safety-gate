# ============================================================
# PATCH for web_agent_v2.1_guardrail.py — Phase 4 Day 2
# Command Graph integration
#
# Apply 3 changes to the existing file:
#   1. Add import at top
#   2. Add graph handler before force_knowledge_routing check
#   3. (Optional) update AVA introduction string — skip for now
# ============================================================


# ── CHANGE 1 ─────────────────────────────────────────────────────────────────
# Add this import alongside the existing secure_executor import
# (around line 18 in the original file)
#
# FIND:
#   from control.secure_executor import execute_command_secure
#
# ADD AFTER:
from control.command_graph import match_graph, execute_graph


# ── CHANGE 2 ─────────────────────────────────────────────────────────────────
# In the /ask route, BEFORE the force_knowledge_routing block.
#
# FIND this block (around line 680):
#
#   # Phase 3: Force KNOWLEDGE routing for how/why/fix queries
#   if force_knowledge_routing(query):
#
# INSERT THIS ENTIRE BLOCK BEFORE IT:

        # ── Phase 4: Command Graph — deterministic diagnostics ──────────────
        graph_name = match_graph(query)
        if graph_name:
            logger.info(f"[*] Command Graph matched: {graph_name}")
            graph_result = execute_graph(graph_name, query)

            # If a medium-risk step needs approval, tell the user
            if graph_result.paused_at:
                elapsed = time.time() - start_time
                return jsonify({
                    'type':        'knowledge',
                    'response':    (
                        f"⚠️ **Approval Required**\n\n"
                        f"I ran the `{graph_name}` diagnostic and reached a step that "
                        f"needs your approval before continuing:\n\n"
                        f"**Tool:** `{graph_result.paused_at}`\n"
                        f"**Approval ID:** `{graph_result.approval_id}`\n\n"
                        f"Run this to approve:\n"
                        f"```bash\npython3 -m control.security_review\n```\n\n"
                        f"Steps completed so far:\n{graph_result.summary_for_ui()}"
                    ),
                    'sources_used': 0,
                    'time_taken':  f"{elapsed:.2f}s",
                    'graph_used':  graph_name,
                })

            # Build context from tool outputs and send to LLM
            context_blocks = graph_result.to_context_blocks()

            # Prepend a framing line so the LLM knows this is live diagnostic data
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


# ── WHERE TO INSERT ───────────────────────────────────────────────────────────
# The block above goes here in the /ask route, immediately before:
#
#   # Phase 3: Force KNOWLEDGE routing for how/why/fix queries
#   if force_knowledge_routing(query):
#       ...
#
# So the final order in /ask is:
#   1. is_greeting check
#   2. *** match_graph (NEW) ***
#   3. force_knowledge_routing
#   4. analyze_query_with_llm
#   5. COMMAND / DIRECT_ANSWER / KNOWLEDGE handlers
