# control/registry.py
# Command whitelist registry

import json
from pathlib import Path

WHITELIST_FILE = "/mnt/i/ai-lab/control_whitelist.json"

# Default approved commands
APPROVED_COMMANDS = [
    "df -h",
    "free -h",
    "ps aux",
    "uptime",
    "docker ps",
    "docker images",
    "systemctl status",
    "git status",
    "ollama list",
    "ls",
    "pwd",
    "whoami",
    "date"
]

def load_whitelist():
    """Load whitelist from file"""
    try:
        if Path(WHITELIST_FILE).exists():
            with open(WHITELIST_FILE, "r") as f:
                return json.load(f)
        else:
            # Create initial whitelist
            save_whitelist(APPROVED_COMMANDS)
            return APPROVED_COMMANDS
    except:
        return APPROVED_COMMANDS

def save_whitelist(commands):
    """Save whitelist to file"""
    with open(WHITELIST_FILE, "w") as f:
        json.dump(commands, f, indent=2)

def is_approved(cmd):
    """Check if command is in whitelist"""
    whitelist = load_whitelist()
    
    # Exact match or prefix match
    for allowed in whitelist:
        if cmd == allowed or cmd.startswith(allowed):
            return True
    
    return False

def add_to_whitelist(cmd):
    """Add command to whitelist"""
    whitelist = load_whitelist()
    
    if cmd not in whitelist:
        whitelist.append(cmd)
        save_whitelist(whitelist)
        return True
    
    return False

def remove_from_whitelist(cmd):
    """Remove command from whitelist"""
    whitelist = load_whitelist()
    
    if cmd in whitelist:
        whitelist.remove(cmd)
        save_whitelist(whitelist)
        return True
    
    return False

def get_whitelist():
    """Get current whitelist"""
    return load_whitelist()
