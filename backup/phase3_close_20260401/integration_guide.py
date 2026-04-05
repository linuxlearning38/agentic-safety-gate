# Integration guide for adding security layer to AVA
# This shows how to modify your web_agent.py to use AgentGuard

"""
INTEGRATION STEPS:

1. Copy these files to your project:
   - control/security_layer.py
   - control/secure_executor.py
   - control/security_review.py

2. Replace your command execution block in web_agent.py
"""

# ============= BEFORE (Your current code) =============

# if decision["intent"] == "command":
#     cmd = decision.get("command")
#     result = run_command(cmd)
#     return jsonify({"result": result})


# ============= AFTER (With AgentGuard) =============

from control.secure_executor import execute_command_secure

@app.route("/ask", methods=["POST"])
def ask():
    data = request.json
    query = data.get("query", "")
    
    # 1. Get intent (your existing code)
    decision = analyze_intent(query)
    
    # 2. COMMAND with security
    if decision["intent"] == "command":
        cmd = decision.get("command")
        
        # Execute with full security controls
        result = execute_command_secure(cmd, query)
        
        # Handle different outcomes
        if result["status"] == "blocked":
            return jsonify({
                "type": "blocked",
                "reason": result["reason"],
                "risk": result["risk"],
                "threats": result["threats"],
                "message": "⛔ Command blocked by security policy"
            })
        
        elif result["status"] == "approval_required":
            return jsonify({
                "type": "approval_required",
                "approval_id": result["approval_id"],
                "command": cmd,
                "risk": result["risk"],
                "threats": result["threats"],
                "message": f"🔒 Command requires approval. Run: python control/security_review.py"
            })
        
        elif result["status"] == "executed":
            # Generate explanation (your existing code)
            explanation = answer(query, result["output"]["stdout"])
            
            return jsonify({
                "type": "command_executed",
                "command": cmd,
                "output": result["output"],
                "explanation": explanation,
                "risk": result["risk"],
                "threats": result["threats"]
            })
    
    # 3. RAG (your existing code stays the same)
    elif decision["intent"] == "rag":
        context = retrieve(query, 8)
        response = answer(query, context)
        return jsonify({"type": "knowledge", "response": response})
    
    # 4. DIRECT (your existing code stays the same)
    else:
        response = llm(query)
        return jsonify({"type": "conversation", "response": response})


# ============= USAGE EXAMPLES =============

"""
Example 1: Safe command (auto-approved)
User: "check disk usage"
AVA: Analyzes → Low risk → Whitelisted → Executes → Returns result

Example 2: Risky command (approval required)
User: "restart nginx"
AVA: Analyzes → High risk → Queues for approval → Returns approval_id

You then run:
$ python control/security_review.py

You see:
REQUEST #1
Command: systemctl restart nginx
Risk Level: HIGH
Blast Radius: service_disruption
Threats: None detected
Recommendation: REQUIRE_APPROVAL

Your decision: a  (approve + whitelist)
✓ Approved and added to permanent whitelist

Example 3: Dangerous command (blocked)
User: "remove all files in root"
AVA: Analyzes → Critical risk → Blocked → Returns error

Response:
{
  "type": "blocked",
  "reason": "Critical risk - command can cause system damage",
  "risk": "critical"
}
"""


# ============= CLI TOOLS =============

"""
Security Review (interactive):
$ python control/security_review.py

View Audit Log:
$ python control/security_review.py audit 50

Check Security Status:
Just run the review tool - it shows status first
"""


# ============= WHAT YOU GET =============

"""
✅ Automatic risk classification
✅ Threat pattern detection  
✅ Safe commands auto-execute
✅ Risky commands require your approval
✅ Dangerous commands blocked
✅ Complete audit trail
✅ Security analytics

This is AgentGuard - enterprise security for AI agents.
"""
