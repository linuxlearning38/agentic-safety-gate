#!/usr/bin/env python3
"""
patch_ui_day6.py — AVA UI Fixes

Changes:
  1. Remove Recent Chats list from sidebar (keep History button)
  2. Add dark/light mode toggle in Settings modal
  3. Add user badge + logout button in sidebar header
"""

import os, shutil
from datetime import datetime

path = "web_agent_v2.1_guardrail.py"

if not os.path.exists(path):
    print(f"ERROR: {path} not found. Run from project root.")
    exit(1)

backup = path + f".backup_ui_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
shutil.copy2(path, backup)
print(f"Backup: {backup}")

with open(path) as f:
    content = f.read()

ok = True

# ── Fix 1: Remove Recent Chats section from sidebar ───────────────────────────
OLD1 = '''            <div class="sidebar-section">
                <div class="sidebar-section-title">Recent Chats</div>
                <div id="recentChats"></div>
            </div>'''

NEW1 = '''            <!-- Recent Chats removed from sidebar — use History button instead -->
            <div id="recentChats" style="display:none;"></div>'''

if OLD1 in content:
    content = content.replace(OLD1, NEW1, 1)
    print("✅  Removed sidebar Recent Chats list")
else:
    print("⚠️  SKIP: Recent Chats anchor not found")
    ok = False

# ── Fix 2: Add user badge + logout to sidebar header ─────────────────────────
OLD2 = '''            <div class="sidebar-header">
                <div class="sidebar-title">AVA</div>
            </div>'''

NEW2 = '''            <div class="sidebar-header">
                <div class="sidebar-title">AVA</div>
                <div id="userBadge" style="
                    margin-top:8px; padding:6px 10px;
                    background:#1a1a2e; border:1px solid #2a2a4a;
                    border-radius:8px; font-size:12px;
                    display:flex; align-items:center; justify-content:space-between;
                ">
                    <span>
                        <span style="color:#667eea;">&#9632;</span>
                        <span id="userBadgeName" style="color:#ccc; margin-left:4px;">...</span>
                        <span id="userBadgeRole" style="
                            margin-left:6px; font-size:10px; padding:2px 6px;
                            border-radius:4px; background:#2a2a4a; color:#888;
                        "></span>
                    </span>
                    <button onclick="logoutAva()" title="Sign out" style="
                        background:none; border:none; color:#555;
                        cursor:pointer; font-size:16px; padding:0 2px; line-height:1;
                    " onmouseover="this.style.color=\'#ff6b6b\'"
                       onmouseout="this.style.color=\'#555\'">&#x23FB;</button>
                </div>
            </div>'''

if OLD2 in content:
    content = content.replace(OLD2, NEW2, 1)
    print("✅  Added user badge + logout button")
else:
    print("⚠️  SKIP: Sidebar header anchor not found")
    ok = False

# ── Fix 3: Add dark/light mode toggle to Settings modal ───────────────────────
OLD3 = '''            <div class="modal-body">
                <div class="stat-card">
                    <div class="stat-label">Model</div>'''

NEW3 = '''            <div class="modal-body">
                <!-- Theme Toggle -->
                <div class="stat-card" style="display:flex; align-items:center; justify-content:space-between;">
                    <div>
                        <div class="stat-label">Theme</div>
                        <div id="themeLabel" style="color:#fff; font-size:13px; margin-top:4px;">Dark Mode</div>
                    </div>
                    <label style="position:relative; display:inline-block; width:48px; height:26px; cursor:pointer;">
                        <input type="checkbox" id="themeToggle" onchange="toggleTheme(this.checked)"
                            style="opacity:0; width:0; height:0;">
                        <span id="themeSlider" style="
                            position:absolute; inset:0; background:#2a2a4a;
                            border-radius:26px; transition:0.3s;
                            display:flex; align-items:center; padding:0 4px;
                            font-size:14px;
                        ">🌙</span>
                    </label>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Model</div>'''

if OLD3 in content:
    content = content.replace(OLD3, NEW3, 1)
    print("✅  Added theme toggle to Settings modal")
else:
    print("⚠️  SKIP: Settings modal anchor not found")
    ok = False

# ── Fix 4: Inject theme JS + update applyRoleUI ──────────────────────────────
OLD4 = "        function applyRoleUI(role) {\n            // Disable execution buttons for readonly users"

NEW4 = """        // ── Theme (dark/light) ────────────────────────────────────────────────
        function toggleTheme(isLight) {
            const root = document.documentElement;
            const label = document.getElementById('themeLabel');
            const slider = document.getElementById('themeSlider');
            if (isLight) {
                root.style.setProperty('--bg-primary',    '#f5f5f5');
                root.style.setProperty('--bg-secondary',  '#ffffff');
                root.style.setProperty('--bg-sidebar',    '#eeeeee');
                root.style.setProperty('--text-primary',  '#111111');
                root.style.setProperty('--text-secondary','#444444');
                root.style.setProperty('--border-color',  '#dddddd');
                document.body.style.background    = '#f5f5f5';
                document.body.style.color         = '#111111';
                if (label)  label.textContent     = 'Light Mode';
                if (slider) slider.textContent    = '☀️';
                if (slider) slider.style.background = '#667eea';
                localStorage.setItem('ava_theme', 'light');
            } else {
                root.style.setProperty('--bg-primary',    '#0a0a0f');
                root.style.setProperty('--bg-secondary',  '#12121f');
                root.style.setProperty('--bg-sidebar',    '#0f0f1a');
                root.style.setProperty('--text-primary',  '#e0e0e0');
                root.style.setProperty('--text-secondary','#888888');
                root.style.setProperty('--border-color',  '#2a2a4a');
                document.body.style.background    = '';
                document.body.style.color         = '';
                if (label)  label.textContent     = 'Dark Mode';
                if (slider) slider.textContent    = '🌙';
                if (slider) slider.style.background = '#2a2a4a';
                localStorage.setItem('ava_theme', 'dark');
            }
        }

        function applyRoleUI(role) {
            // Update user badge
            const nameEl = document.getElementById('userBadgeName');
            const roleEl = document.getElementById('userBadgeRole');
            if (nameEl) nameEl.textContent = window._avaUser || '';
            if (roleEl) {
                roleEl.textContent     = role;
                roleEl.style.background = role === 'admin' ? '#1a3a1a' : '#2a2a4a';
                roleEl.style.color      = role === 'admin' ? '#4caf50' : '#888';
            }
            // Restore saved theme
            const savedTheme = localStorage.getItem('ava_theme');
            if (savedTheme === 'light') {
                const tog = document.getElementById('themeToggle');
                if (tog) { tog.checked = true; toggleTheme(true); }
            }
            // Disable execution buttons for readonly users"""

if OLD4 in content:
    content = content.replace(OLD4, NEW4, 1)
    print("✅  Injected theme JS + updated applyRoleUI")
else:
    print("⚠️  SKIP: applyRoleUI anchor not found")
    ok = False

with open(path, "w") as f:
    f.write(content)

print()
if ok:
    print("✅  All UI patches applied.")
else:
    print("⚠️  Some patches skipped — check output above.")
print("Restart AVA to see changes.")
