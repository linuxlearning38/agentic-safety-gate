# control/approval.py
# Approval queue management

import json
import uuid
from datetime import datetime, timedelta

from control.registry import normalize_command_signature
from control.runtime_paths import get_runtime_path


def _queue_file():
    return get_runtime_path("APPROVAL_QUEUE_PATH", "approval_queue.json")

def load_queue():
    """Load approval queue from file"""
    try:
        queue_file = _queue_file()
        if queue_file.exists():
            with open(queue_file, "r") as f:
                return json.load(f)
        else:
            return []
    except:
        return []

def save_queue(queue):
    """Save approval queue to file"""
    queue_file = _queue_file()
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    with open(queue_file, "w") as f:
        json.dump(queue, f, indent=2)

def add_request(cmd, query, *, risk=None, mode=None, approval_key=None, metadata=None):
    """Add command to approval queue"""
    queue = load_queue()
    
    entry = {
        "id": str(uuid.uuid4())[:8],  # Shorter ID for convenience
        "timestamp": datetime.now().isoformat(),
        "command": cmd,
        "query": query,
        "status": "pending",
        "risk": risk,
        "mode": mode,
        "approval_key": normalize_command_signature(approval_key or cmd),
        "metadata": metadata or {},
    }
    
    queue.append(entry)
    save_queue(queue)
    
    return entry["id"]

def get_pending():
    """Get all pending approval requests"""
    queue = load_queue()
    return [q for q in queue if q["status"] == "pending"]

def get_by_id(entry_id):
    """Get approval request by ID"""
    queue = load_queue()
    return next((q for q in queue if q["id"] == entry_id), None)

def update_status(entry_id, status):
    """Update approval request status"""
    queue = load_queue()
    
    for q in queue:
        if q["id"] == entry_id:
            q["status"] = status
            q["updated"] = datetime.now().isoformat()
    
    save_queue(queue)
    return True


def check_recent_approval(approval_key: str, minutes: int = 10):
    """Check whether an approval key was approved recently."""
    normalized_key = normalize_command_signature(approval_key)
    queue = load_queue()
    cutoff = datetime.now() - timedelta(minutes=minutes)
    for entry in queue:
        entry_key = normalize_command_signature(entry.get("approval_key") or entry.get("command"))
        if (
            entry_key == normalized_key
            and entry.get("status") == "approved"
            and datetime.fromisoformat(entry["timestamp"]) > cutoff
        ):
            return True, entry["id"]
    return False, None

def clear_old_entries(days=7):
    """Remove entries older than specified days"""
    queue = load_queue()
    cutoff = datetime.now() - timedelta(days=days)
    
    filtered = [
        q for q in queue 
        if datetime.fromisoformat(q["timestamp"]) > cutoff
    ]
    
    save_queue(filtered)
    return len(queue) - len(filtered)
