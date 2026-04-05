#!/usr/bin/env python3
"""
Enhanced Security Review Tool with CSV Export
"""

import sys
import json
import os
from datetime import datetime, timedelta
from control.approval import get_pending, update_status, load_queue
from control.registry import add_to_whitelist

AUDIT_LOG_PATH = "/mnt/i/ai-lab/security_audit.json"

def load_audit_log():
    """Load audit log"""
    if not os.path.exists(AUDIT_LOG_PATH):
        return []
    
    with open(AUDIT_LOG_PATH, 'r') as f:
        return json.load(f)

def get_security_stats(hours=24):
    """Get security statistics for dashboard"""
    audit_log = load_audit_log()
    cutoff = datetime.now() - timedelta(hours=hours)
    
    # Filter to last N hours
    recent = [
        entry for entry in audit_log
        if datetime.fromisoformat(entry['timestamp']) > cutoff
    ]
    
    stats = {
        'total_commands': len(recent),
        'executed': len([e for e in recent if e['event_type'] == 'executed']),
        'blocked': len([e for e in recent if e['event_type'] == 'blocked']),
        'queued': len([e for e in recent if e['event_type'] == 'queued_for_approval']),
        'approved': len([e for e in recent if e['event_type'] == 'approved']),
        'high_risk': len([e for e in recent if e.get('risk_analysis', {}).get('risk') in ['high', 'critical']]),
        'threats_detected': sum(len(e.get('threats', [])) for e in recent),
        'threat_types': {}
    }
    
    # Count threat types
    for entry in recent:
        for threat in entry.get('threats', []):
            threat_type = threat.get('type', 'unknown')
            stats['threat_types'][threat_type] = stats['threat_types'].get(threat_type, 0) + 1
    
    return stats

def export_audit_to_csv(output_file, days=7, risk_filter=None, decision_filter=None):
    """Export audit log to CSV"""
    import csv
    
    audit_log = load_audit_log()
    cutoff = datetime.now() - timedelta(days=days)
    
    # Filter
    filtered = [
        entry for entry in audit_log
        if datetime.fromisoformat(entry['timestamp']) > cutoff
    ]
    
    if risk_filter:
        filtered = [e for e in filtered if e.get('risk_analysis', {}).get('risk') == risk_filter]
    
    if decision_filter:
        filtered = [e for e in filtered if e.get('decision') == decision_filter]
    
    # Write CSV
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        
        # Header
        writer.writerow([
            'Timestamp', 'Event Type', 'Command', 'Query', 
            'Risk Level', 'Decision', 'Threats Count', 'Threat Types'
        ])
        
        # Data
        for entry in filtered:
            threat_types = ', '.join([t.get('type', '') for t in entry.get('threats', [])])
            
            writer.writerow([
                entry['timestamp'],
                entry['event_type'],
                entry.get('cmd', ''),
                entry.get('query', ''),
                entry.get('risk_analysis', {}).get('risk', ''),
                entry.get('decision', ''),
                len(entry.get('threats', [])),
                threat_types
            ])
    
    return len(filtered)

def show_dashboard():
    """Show security dashboard"""
    stats = get_security_stats(24)
    pending = get_pending()
    
    print("\n" + "="*60)
    print("AGENTGUARD - SECURITY STATUS (Last 24h)")
    print("="*60)
    print(f"Total Commands: {stats['total_commands']}")
    print(f"Blocked: {stats['blocked']}")
    print(f"Approved: {stats['approved']}")
    print(f"Threats Detected: {stats['threats_detected']}")
    print(f"High Risk Commands: {stats['high_risk']}")
    
    if stats['threat_types']:
        print("\nThreat Types Detected:")
        for threat_type, count in sorted(stats['threat_types'].items(), key=lambda x: x[1], reverse=True):
            print(f"  • {threat_type}: {count}")
    
    print("="*60)
    print()
    
    if not pending:
        print("✓ No pending approvals\n")
        return
    
    print(f"Pending Approvals: {len(pending)}\n")

def review_approvals():
    """Review pending approvals"""
    show_dashboard()
    
    pending = get_pending()
    if not pending:
        return
    
    for idx, entry in enumerate(pending, 1):
        print("-" * 60)
        print(f"REQUEST #{idx}")
        print("-" * 60)
        print(f"ID: {entry['id']}")
        print(f"Query: {entry['query']}")
        print(f"Command: {entry['command']}")
        print(f"Time: {entry['timestamp']}")
        
        # Show security analysis
        if 'security_analysis' in entry:
            analysis = entry['security_analysis']
            print("\n[SECURITY ANALYSIS]")
            print(f"Risk Level: {analysis.get('risk_analysis', {}).get('risk', 'unknown').upper()}")
            print(f"Blast Radius: {analysis.get('risk_analysis', {}).get('blast_radius', 'unknown')}")
            print(f"Description: {analysis.get('risk_analysis', {}).get('description', 'N/A')}")
            
            threats = analysis.get('threats', [])
            if threats:
                print(f"\n⚠️  Threats Detected: {len(threats)}")
                for threat in threats[:3]:  # Show first 3
                    print(f"  • {threat.get('type', 'unknown')} (severity: {threat.get('severity', 'unknown')})")
            else:
                print("\n✓ No threats detected")
            
            print(f"\nRecommendation: {analysis.get('recommendation', 'unknown').upper()}")
            print(f"Reason: {analysis.get('reason', 'N/A')}")
        
        print("-" * 60)
        print("OPTIONS:")
        print("  y = Approve (execute once)")
        print("  a = Approve + Add to whitelist (auto-approve future)")
        print("  n = Reject")
        print("  s = Skip (decide later)")
        print("-" * 60)
        
        decision = input("\nYour decision: ").lower().strip()
        
        if decision == 'y':
            update_status(entry['id'], 'approved')
            print("✓ Approved for one-time execution")
        
        elif decision == 'a':
            # Check risk level
            risk = entry.get('security_analysis', {}).get('risk_analysis', {}).get('risk', 'unknown')
            if risk in ['high', 'critical']:
                confirm = input(f"⚠️  WARNING: This is a {risk.upper()} risk command. Really add to whitelist? (yes/no): ")
                if confirm.lower() != 'yes':
                    print("✗ Cancelled")
                    continue
            
            add_to_whitelist(entry['command'])
            update_status(entry['id'], 'approved')
            print("✓ Approved and added to permanent whitelist")
        
        elif decision == 'n':
            update_status(entry['id'], 'rejected')
            print("✗ Rejected")
        
        elif decision == 's':
            print("→ Skipped")
        
        else:
            print("✗ Invalid choice")
        
        print()
    
    print("="*60)
    print("Review complete")
    print("="*60)

def show_audit_log(count=10):
    """Show recent audit log entries"""
    audit_log = load_audit_log()
    
    print("\n" + "="*60)
    print(f"SECURITY AUDIT LOG (Last {count} entries)")
    print("="*60)
    print()
    
    recent = audit_log[-count:]
    
    event_icons = {
        'executed': '✓',
        'blocked': '✗',
        'queued_for_approval': '⊙',
        'approved': '→'
    }
    
    for entry in recent:
        icon = event_icons.get(entry['event_type'], '•')
        event_type = entry['event_type'].upper().replace('_', ' ')
        timestamp = entry['timestamp'][:19]  # Remove microseconds
        
        print(f"{icon} [{timestamp}] {event_type}")
        print(f"  Command: {entry.get('cmd', 'N/A')}")
        
        risk = entry.get('risk_analysis', {}).get('risk', 'unknown')
        decision = entry.get('decision', 'unknown')
        print(f"  Risk: {risk.upper()}, Decision: {decision}")
        
        print()

def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        review_approvals()
        return
    
    command = sys.argv[1]
    
    if command == 'audit':
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        show_audit_log(count)
    
    elif command == 'export':
        output_file = sys.argv[2] if len(sys.argv) > 2 else '/mnt/i/ai-lab/audit_export.csv'
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 7
        
        print(f"\nExporting audit log to: {output_file}")
        print(f"Date range: Last {days} days\n")
        
        count = export_audit_to_csv(output_file, days=days)
        
        print(f"✓ Exported {count} entries to {output_file}\n")
    
    elif command == 'stats':
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        stats = get_security_stats(hours)
        
        print(f"\nSecurity Statistics (Last {hours} hours):")
        print(json.dumps(stats, indent=2))
        print()
    
    else:
        print(f"Unknown command: {command}")
        print("Usage:")
        print("  python3 -m control.security_review          # Review approvals")
        print("  python3 -m control.security_review audit 10  # Show last 10 audit entries")
        print("  python3 -m control.security_review export [file] [days]  # Export to CSV")
        print("  python3 -m control.security_review stats [hours]  # Show statistics")

if __name__ == '__main__':
    main()
