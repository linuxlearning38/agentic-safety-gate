#!/usr/bin/env python3
"""
Fix AVA's hallucinated execution responses
Makes command status crystal clear - no fake success messages
"""

import sys

def apply_fix(filename):
    print(f"🔧 Fixing hallucinated responses in {filename}...")
    
    with open(filename, 'r') as f:
        content = f.read()
    
    changes_made = 0
    
    # Fix 1: Add clear status message when command is pending approval
    # Find the part where status is 'pending_approval' or 'require_approval'
    old_pending_msg = '"error": "High risk operation requires manual approval"'
    new_pending_msg = '"error": "🛡️ SECURITY APPROVAL REQUIRED\\n\\nThis command queued for manual review.\\n\\n📋 To approve/reject:\\n   python3 -m control.security_review\\n\\n🔴 Check Security dashboard (red badge shows pending count)"'
    
    if old_pending_msg in content:
        content = content.replace(old_pending_msg, new_pending_msg)
        print("  ✓ Enhanced pending approval message")
        changes_made += 1
    
    # Fix 2: Prevent LLM from generating fake success messages
    # Add instruction to the system prompt
    
    system_prompt_marker = 'You are AVA, a DevOps AI assistant'
    if system_prompt_marker in content:
        enhanced_prompt = '''You are AVA, a DevOps AI assistant with security controls.

CRITICAL INSTRUCTIONS FOR COMMAND EXECUTION:
1. When executing commands, you will receive ACTUAL output from the system
2. NEVER say "command executed successfully" unless you see real output
3. If you see "requires manual approval", tell user: "Command queued for approval - check Security dashboard"
4. Do NOT predict or imagine command results
5. Only report what you actually observe from system output

Your responses should be based on FACTS, not predictions.'''
        
        content = content.replace(system_prompt_marker, enhanced_prompt)
        print("  ✓ Enhanced system prompt to prevent hallucinations")
        changes_made += 1
    
    # Fix 3: Make execution output clearer
    old_exec_msg = '"output": command_output'
    new_exec_msg = '"output": "✅ EXECUTED\\n\\n" + command_output'
    
    if old_exec_msg in content:
        content = content.replace(old_exec_msg, new_exec_msg)
        print("  ✓ Added execution indicator to output")
        changes_made += 1
    
    # Write the fixed file
    with open(filename, 'w') as f:
        f.write(content)
    
    if changes_made > 0:
        print(f"\n✅ Applied {changes_made} fixes successfully!")
        print(f"\n📝 Changes:")
        print("  • Enhanced 'pending approval' error message")
        print("  • Added anti-hallucination instruction to LLM")
        print("  • Clearer execution output formatting")
        return True
    else:
        print("\n⚠️  No changes made - patterns not found")
        print("File may already be patched or structure is different")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 fix_hallucinated_responses.py <file.py>")
        print("\nExample:")
        print("  python3 fix_hallucinated_responses.py web_agent_v2.1_guardrail.py")
        sys.exit(1)
    
    filename = sys.argv[1]
    
    print(f"📥 Input: {filename}\n")
    
    if apply_fix(filename):
        print("\n🎉 DONE! Restart AVA to see changes:")
        print("  lsof -ti:5002 | xargs kill -9")
        print("  source venv/bin/activate")
        print(f"  python3 {filename}")
        print("\nNow AVA won't hallucinate execution results! ✅")
    else:
        print("\n❌ Fix failed or already applied")
        sys.exit(1)

if __name__ == '__main__':
    main()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 fix_hallucinated_responses.py <file.py>")
        sys.exit(1)
    
    filename = sys.argv[1]
    
    print(f"📥 Input: {filename}\n")
    
    if apply_fix(filename):
        print("\n🎉 DONE! Restart AVA to see the changes:")
        print("  lsof -ti:5002 | xargs kill -9")
        print("  source venv/bin/activate")
        print("  python3 web_agent_v2.1_guardrail.py")
    else:
        print("\n❌ Fix failed")
        sys.exit(1)

if __name__ == '__main__':
    main()
