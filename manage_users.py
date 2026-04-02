#!/usr/bin/env python3
"""
manage_users.py — AVA User Management CLI
Day 5 — Phase 4

Usage:
  python3 manage_users.py list
  python3 manage_users.py add <username> <password> <role>
  python3 manage_users.py change-password <username> <new_password>
  python3 manage_users.py delete <username>
  python3 manage_users.py change-role <username> <new_role>

Roles: admin, readonly
Passwords are bcrypt hashed — plaintext never stored.
"""

import os
import sys
import json
import shutil
import bcrypt
from datetime import datetime

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
VALID_ROLES = {"admin", "readonly"}


def _load() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            return json.load(f)
    return {}


def _save(data: dict):
    backup = USERS_FILE + f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if os.path.exists(USERS_FILE):
        shutil.copy2(USERS_FILE, backup)
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"✅  Saved: {USERS_FILE}")


def _hash(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def cmd_list():
    users = _load()
    if not users:
        print("No users found.")
        return
    print(f"\n{'USERNAME':<20} {'ROLE':<12}")
    print("-" * 32)
    for username, info in users.items():
        print(f"{username:<20} {info.get('role','?'):<12}")
    print(f"\nTotal: {len(users)} user(s)")


def cmd_add(username: str, password: str, role: str):
    if role not in VALID_ROLES:
        print(f"❌  Invalid role '{role}'. Valid roles: {VALID_ROLES}")
        sys.exit(1)
    users = _load()
    if username in users:
        print(f"❌  User '{username}' already exists. Use change-password or change-role.")
        sys.exit(1)
    print(f"⏳  Hashing password (bcrypt rounds=12)…")
    users[username] = {"password_hash": _hash(password), "role": role}
    _save(users)
    print(f"✅  User '{username}' created with role '{role}'")


def cmd_change_password(username: str, new_password: str):
    users = _load()
    if username not in users:
        print(f"❌  User '{username}' not found.")
        sys.exit(1)
    print(f"⏳  Hashing new password…")
    users[username]["password_hash"] = _hash(new_password)
    _save(users)
    print(f"✅  Password updated for '{username}'")


def cmd_delete(username: str):
    users = _load()
    if username not in users:
        print(f"❌  User '{username}' not found.")
        sys.exit(1)
    confirm = input(f"Delete user '{username}'? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return
    del users[username]
    _save(users)
    print(f"✅  User '{username}' deleted")


def cmd_change_role(username: str, new_role: str):
    if new_role not in VALID_ROLES:
        print(f"❌  Invalid role '{new_role}'. Valid roles: {VALID_ROLES}")
        sys.exit(1)
    users = _load()
    if username not in users:
        print(f"❌  User '{username}' not found.")
        sys.exit(1)
    old_role = users[username].get("role")
    users[username]["role"] = new_role
    _save(users)
    print(f"✅  '{username}' role changed: {old_role} → {new_role}")


COMMANDS = {
    "list":            (cmd_list,            0, ""),
    "add":             (cmd_add,             3, "<username> <password> <role>"),
    "change-password": (cmd_change_password, 2, "<username> <new_password>"),
    "delete":          (cmd_delete,          1, "<username>"),
    "change-role":     (cmd_change_role,     2, "<username> <new_role>"),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("Commands:")
        for name, (_, nargs, args) in COMMANDS.items():
            print(f"  {name} {args}")
        sys.exit(1)

    cmd  = sys.argv[1]
    fn, nargs, usage = COMMANDS[cmd]
    args = sys.argv[2:]

    if len(args) != nargs:
        print(f"❌  Usage: python3 manage_users.py {cmd} {usage}")
        sys.exit(1)

    fn(*args)


if __name__ == "__main__":
    main()
