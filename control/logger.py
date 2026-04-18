# control/logger.py
# Execution logging

import json
from datetime import datetime

from control.runtime_paths import get_runtime_path


def _log_file():
    return get_runtime_path("EXECUTION_LOG_PATH", "execution_log.json")

def log(query, cmd, result):
    """Log command execution"""
    if isinstance(result, dict):
        output = result.get("output", "")
        error = result.get("error", "")
        if isinstance(output, dict):
            stdout = output.get("stdout", "")
            stderr = output.get("stderr", "")
            returncode = output.get("returncode", -1)
        else:
            stdout = output or ""
            stderr = error or ""
            status = result.get("status", "")
            returncode = 0 if status == "success" else -1
    else:
        stdout = ""
        stderr = str(result)
        returncode = -1

    entry = {
        "timestamp": datetime.now().isoformat(),
        "query": query,
        "command": cmd,
        "result": {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
        },
        "success": returncode == 0,
    }
    
    # Load existing logs
    try:
        log_file = _log_file()
        if log_file.exists():
            with open(log_file, "r") as f:
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
    
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w") as f:
        json.dump(logs, f, indent=2)

def get_recent_logs(n=50):
    """Get recent execution logs"""
    try:
        with open(_log_file(), "r") as f:
            logs = json.load(f)
        return logs[-n:]
    except:
        return []

def get_failed_executions(hours=24):
    """Get failed executions in last N hours"""
    from datetime import timedelta
    
    try:
        with open(_log_file(), "r") as f:
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
