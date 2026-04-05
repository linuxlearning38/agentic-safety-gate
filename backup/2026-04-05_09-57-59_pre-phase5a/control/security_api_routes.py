# Add these routes to your Flask app (web_agent_v2.1.py)

@app.route('/security/stats', methods=['GET'])
def get_security_stats():
    """Get security statistics for dashboard"""
    try:
        from control.approval import get_pending, load_queue
        import json
        from datetime import datetime, timedelta
        
        # Load audit log
        audit_log_path = "/mnt/i/ai-lab/security_audit.json"
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
            'threats_detected': sum(len(e.get('threats', [])) for e in recent),
            'recent_audit': recent[-10:]  # Last 10 entries
        }
        
        return jsonify(stats)
        
    except Exception as e:
        logger.error(f"Error getting security stats: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/security/pending', methods=['GET'])
def get_pending_approvals():
    """Get pending approval requests"""
    try:
        from control.approval import get_pending
        
        pending = get_pending()
        
        return jsonify({
            'count': len(pending),
            'requests': pending
        })
        
    except Exception as e:
        logger.error(f"Error getting pending approvals: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/security/audit', methods=['GET'])
def get_audit_log():
    """Get audit log entries"""
    try:
        count = int(request.args.get('count', 20))
        
        audit_log_path = "/mnt/i/ai-lab/security_audit.json"
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
