# control/registry.py
# Command whitelist registry

import json
import shlex

from control.runtime_paths import get_runtime_path

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


def normalize_command_signature(cmd):
    """Normalize command text for stable whitelist and approval matching."""
    if not isinstance(cmd, str):
        return ""
    text = cmd.strip()
    if not text:
        return ""
    try:
        return shlex.join(shlex.split(text))
    except Exception:
        return " ".join(text.split())

def load_whitelist():
    """Load whitelist from file"""
    try:
        whitelist_file = get_runtime_path("WHITELIST_PATH", "control_whitelist.json")
        if whitelist_file.exists():
            with open(whitelist_file, "r") as f:
                return json.load(f)
        else:
            # Create initial whitelist
            save_whitelist(APPROVED_COMMANDS)
            return APPROVED_COMMANDS
    except:
        return APPROVED_COMMANDS

def save_whitelist(commands):
    """Save whitelist to file"""
    whitelist_file = get_runtime_path("WHITELIST_PATH", "control_whitelist.json")
    whitelist_file.parent.mkdir(parents=True, exist_ok=True)
    with open(whitelist_file, "w") as f:
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
    normalized_cmd = normalize_command_signature(cmd)
    if not normalized_cmd:
        return False
    for allowed in whitelist:
        normalized_allowed = normalize_command_signature(allowed)
        allowed_tokens = normalized_allowed.split()
        if not allowed_tokens:
            continue
        if len(allowed_tokens) == 1:
            # Single-token: must be an exact match
            if normalized_cmd == normalized_allowed:
                return True
        else:
            # Multi-token: exact match or cmd adds further args after a space
            if normalized_cmd == normalized_allowed or normalized_cmd.startswith(normalized_allowed + " "):
                return True
    return False

def add_to_whitelist(cmd):
    """Add command to whitelist"""
    whitelist = load_whitelist()

    normalized_cmd = normalize_command_signature(cmd)
    if not normalized_cmd:
        return False

    normalized_existing = {normalize_command_signature(entry) for entry in whitelist}
    if normalized_cmd not in normalized_existing:
        whitelist.append(normalized_cmd)
        save_whitelist(whitelist)
        return True
    
    return False

def remove_from_whitelist(cmd):
    """Remove command from whitelist"""
    whitelist = load_whitelist()

    normalized_cmd = normalize_command_signature(cmd)
    filtered = [entry for entry in whitelist if normalize_command_signature(entry) != normalized_cmd]

    if len(filtered) != len(whitelist):
        save_whitelist(filtered)
        return True

    if cmd in whitelist:
        whitelist.remove(cmd)
        save_whitelist(whitelist)
        return True
    
    return False

def get_whitelist():
    """Get current whitelist"""
    return load_whitelist()
