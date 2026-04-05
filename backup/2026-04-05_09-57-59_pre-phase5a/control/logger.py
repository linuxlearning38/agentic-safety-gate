# control/logger.py
# Execution logging

import json
from datetime import datetime
from pathlib import Path

LOG_FILE = "/mnt/i/ai-lab/execution_log.json"

def log(query, cmd, result):
    """Log command execution"""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "query": query,
        "command": cmd,
        "result": {
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "returncode": result.get("returncode", -1)
        },
        "success": result.get("returncode", -1) == 0
    }
    
    # Load existing logs
    try:
        if Path(LOG_FILE).exists():
            with open(LOG_FILE, "r") as f:
                logs = json.load(f)
        else:
            logs = []
    except:
        logs = []
    
    # Append and save
    logs.append(entry)
    
    # Keep only last 1000 entries
    if len(logs) > 1000:
        logs = logs[-1000:]
    
    with open(LOG_FILE, "w") as f:
        json.dump(logs, f, indent=2)

def get_recent_logs(n=50):
    """Get recent execution logs"""
    try:
        with open(LOG_FILE, "r") as f:
            logs = json.load(f)
        return logs[-n:]
    except:
        return []

def get_failed_executions(hours=24):
    """Get failed executions in last N hours"""
    from datetime import timedelta
    
    try:
        with open(LOG_FILE, "r") as f:
            logs = json.load(f)
    except:
        return []
    
    cutoff = datetime.now() - timedelta(hours=hours)
    
    failed = [
        log for log in logs
        if not log.get("success", True)
        and datetime.fromisoformat(log["timestamp"]) > cutoff
    ]
    
    return failed
