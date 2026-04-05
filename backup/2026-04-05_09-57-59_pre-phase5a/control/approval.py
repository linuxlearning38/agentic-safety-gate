# control/approval.py
# Approval queue management

import json
import uuid
from datetime import datetime
from pathlib import Path

QUEUE_FILE = "/mnt/i/ai-lab/approval_queue.json"

def load_queue():
    """Load approval queue from file"""
    try:
        if Path(QUEUE_FILE).exists():
            with open(QUEUE_FILE, "r") as f:
                return json.load(f)
        else:
            return []
    except:
        return []

def save_queue(queue):
    """Save approval queue to file"""
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)

def add_request(cmd, query):
    """Add command to approval queue"""
    queue = load_queue()
    
    entry = {
        "id": str(uuid.uuid4())[:8],  # Shorter ID for convenience
        "timestamp": datetime.now().isoformat(),
        "command": cmd,
        "query": query,
        "status": "pending"
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

def clear_old_entries(days=7):
    """Remove entries older than specified days"""
    from datetime import timedelta
    
    queue = load_queue()
    cutoff = datetime.now() - timedelta(days=days)
    
    filtered = [
        q for q in queue 
        if datetime.fromisoformat(q["timestamp"]) > cutoff
    ]
    
    save_queue(filtered)
    return len(queue) - len(filtered)
