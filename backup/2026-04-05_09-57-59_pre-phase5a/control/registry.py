# control/registry.py
# Command whitelist registry

import json
from pathlib import Path

WHITELIST_FILE = "/home/manoj/ava-data/control_whitelist.json"

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
    """Check if command is in whitelist.

    Matching rules:
    - Single-token entry (e.g. "ls", "git"): exact match only.
      "ls" does NOT approve "ls /etc/shadow".
    - Multi-token entry (e.g. "git status", "docker ps"): exact match OR
      cmd extends the pattern with additional args ("git status --short" is
      approved by "git status", but "git statusfoo" is not).
    """
    whitelist = load_whitelist()
    if not cmd:
        return False
    for allowed in whitelist:
        allowed_tokens = allowed.split()
        if not allowed_tokens:
            continue
        if len(allowed_tokens) == 1:
            # Single-token: must be an exact match
            if cmd == allowed:
                return True
        else:
            # Multi-token: exact match or cmd adds further args after a space
            if cmd == allowed or cmd.startswith(allowed + " "):
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
