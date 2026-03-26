#!/usr/bin/env python3
"""
Automatic patcher to add Security Dashboard UI to AVA
Applies all Phase 1 Polish enhancements
"""

import sys
import os

def patch_web_agent(input_file, output_file):
    """Apply security UI patches to web_agent.py"""
    
    print("🔧 Patching AVA with Security Dashboard UI...")
    
    with open(input_file, 'r') as f:
        content = f.read()
    
    # 1. Add security routes after get_stats() route
    security_routes = '''
@app.route('/security/stats', methods=['GET'])
def get_security_stats_route():
    """Get security statistics for dashboard"""
    try:
        from control.approval import get_pending
        from datetime import timedelta
        
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
            'threats_detected': sum(len(e.get('threats', [])) for e in recent)
        }
        
        return jsonify(stats)
        
    except Exception as e:
        logger.error(f"Error getting security stats: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/security/audit', methods=['GET'])
def get_audit_log_route():
    """Get audit log entries"""
    try:
        count = int(request.args.get('count', 10))
        
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

'''
    
    # Find where to insert routes (before HTML_TEMPLATE)
    html_template_marker = "# HTML Template\nHTML_TEMPLATE = r'''"
    if html_template_marker in content:
        content = content.replace(html_template_marker, security_routes + html_template_marker)
        print("  ✓ Added security API routes")
    else:
        print("  ✗ Could not find HTML_TEMPLATE marker")
        return False
    
    # 2. Add security button to sidebar (after Settings button)
    settings_button = '''            <button class="sidebar-btn" onclick="showSettingsModal()">
                <span>⚙️</span>
                <span>Settings</span>
            </button>'''
    
    security_button = '''            <button class="sidebar-btn" onclick="showSettingsModal()">
                <span>⚙️</span>
                <span>Settings</span>
            </button>
            
            <button class="sidebar-btn" onclick="showSecurityModal()">
                <span>🛡️</span>
                <span>Security</span>
                <span id="securityBadge" class="badge" style="display: none;"></span>
            </button>'''
    
    if settings_button in content:
        content = content.replace(settings_button, security_button)
        print("  ✓ Added security button to sidebar")
    else:
        print("  ✗ Could not find settings button")
    
    # 3. Add badge CSS (after existing CSS, before </style>)
    badge_css = '''        
        /* Security Badge & Dashboard Styles */
        .badge {
            background: #ff4444;
            color: white;
            border-radius: 10px;
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 600;
            margin-left: auto;
        }
        
        .security-stat {
            display: flex;
            justify-content: space-between;
            padding: 12px 16px;
            background: #242424;
            border-radius: 8px;
            margin-bottom: 8px;
        }
        
        .security-stat-label {
            color: #aaa;
            font-size: 14px;
        }
        
        .security-stat-value {
            color: #667eea;
            font-weight: 600;
            font-size: 14px;
        }
        
        .security-stat-value.danger {
            color: #ff6b6b;
        }
        
        .security-stat-value.success {
            color: #51cf66;
        }
        
        .audit-entry {
            padding: 12px;
            background: #242424;
            border-radius: 6px;
            margin-bottom: 8px;
            border-left: 3px solid #667eea;
            font-size: 13px;
        }
        
        .audit-entry.blocked {
            border-left-color: #ff6b6b;
        }
        
        .audit-entry.executed {
            border-left-color: #51cf66;
        }
        
        .audit-time {
            color: #888;
            font-size: 11px;
            margin-bottom: 4px;
        }
        
        .audit-command {
            color: #fff;
            font-family: 'Monaco', monospace;
            margin-bottom: 4px;
        }
        
        .audit-risk {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            margin-right: 8px;
        }
        
        .audit-risk.high {
            background: #ff6b6b22;
            color: #ff6b6b;
        }
        
        .audit-risk.low {
            background: #51cf6622;
            color: #51cf66;
        }
    </style>'''
    
    style_end = "    </style>"
    if style_end in content:
        content = content.replace(style_end, badge_css)
        print("  ✓ Added security CSS styles")
    else:
        print("  ✗ Could not find </style> tag")
    
    # 4. Add security modal (after settings modal, before </body>)
    security_modal = '''    
    <!-- Security Modal -->
    <div id="securityModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <span class="modal-title">🛡️ Security Dashboard</span>
                <button class="modal-close" onclick="closeModal('securityModal')">&times;</button>
            </div>
            <div class="modal-body" id="securityContent">
                <p style="color: #888;">Loading...</p>
            </div>
        </div>
    </div>
    
    <script>'''
    
    script_start = "    <script>"
    if script_start in content:
        content = content.replace(script_start, security_modal, 1)
        print("  ✓ Added security modal")
    else:
        print("  ✗ Could not find <script> tag")
    
    # 5. Add JavaScript functions (before </script>)
    security_js = '''        
        // Security Dashboard Functions
        function showSecurityModal() {
            document.getElementById('securityModal').style.display = 'block';
            loadSecurityData();
        }
        
        function loadSecurityData() {
            Promise.all([
                fetch('/security/stats').then(r => r.json()),
                fetch('/security/audit?count=10').then(r => r.json())
            ])
            .then(([stats, audit]) => {
                displaySecurityData(stats, audit);
            })
            .catch(err => {
                console.error('Error loading security data:', err);
                document.getElementById('securityContent').innerHTML = 
                    '<p style="color: #ff6b6b;">Error loading security data</p>';
            });
        }
        
        function displaySecurityData(stats, audit) {
            const content = document.getElementById('securityContent');
            
            let html = '<div style="margin-bottom: 24px;">';
            html += '<h3 style="margin-bottom: 12px; font-size: 14px; color: #888; text-transform: uppercase;">Last 24 Hours</h3>';
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Total Commands</span>
                <span class="security-stat-value">${stats.total_commands}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Executed</span>
                <span class="security-stat-value success">${stats.executed}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Blocked</span>
                <span class="security-stat-value ${stats.blocked > 0 ? 'danger' : ''}">$ {stats.blocked}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Pending Approval</span>
                <span class="security-stat-value ${stats.pending > 0 ? 'danger' : ''}">$ {stats.pending}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">High Risk Commands</span>
                <span class="security-stat-value">${stats.high_risk}</span>
            </div>`;
            
            html += `<div class="security-stat">
                <span class="security-stat-label">Threats Detected</span>
                <span class="security-stat-value ${stats.threats_detected > 0 ? 'danger' : ''}">$ {stats.threats_detected}</span>
            </div>`;
            
            html += '</div>';
            
            // Recent audit entries
            html += '<div>';
            html += '<h3 style="margin-bottom: 12px; font-size: 14px; color: #888; text-transform: uppercase;">Recent Activity</h3>';
            
            if (audit.entries && audit.entries.length > 0) {
                audit.entries.reverse().forEach(entry => {
                    const eventClass = entry.event_type === 'blocked' ? 'blocked' : 
                                      entry.event_type === 'executed' ? 'executed' : '';
                    const risk = entry.risk_analysis?.risk || 'unknown';
                    const riskClass = risk === 'high' || risk === 'critical' ? 'high' : 'low';
                    
                    html += `<div class="audit-entry ${eventClass}">
                        <div class="audit-time">${new Date(entry.timestamp).toLocaleString()}</div>
                        <div class="audit-command">${escapeHtml(entry.cmd || 'N/A')}</div>
                        <div>
                            <span class="audit-risk ${riskClass}">${risk.toUpperCase()}</span>
                            <span style="color: #888; font-size: 11px;">${entry.event_type.replace(/_/g, ' ').toUpperCase()}</span>
                        </div>
                    </div>`;
                });
            } else {
                html += '<p style="color: #888; text-align: center;">No recent activity</p>';
            }
            
            html += '</div>';
            
            // CLI instructions
            html += `<div style="margin-top: 24px; padding: 16px; background: #1a1a2e; border-radius: 8px; border-left: 3px solid #667eea;">
                <div style="font-size: 12px; color: #667eea; margin-bottom: 6px; font-weight: 600;">📋 CLI Commands</div>
                <div style="color: #ddd; font-size: 13px; line-height: 1.8;">
                    <code style="background: #0a0a0a; padding: 2px 6px; border-radius: 3px;">python3 -m control.security_review</code> - Review pending<br>
                    <code style="background: #0a0a0a; padding: 2px 6px; border-radius: 3px;">python3 -m control.security_review audit 10</code> - View audit log<br>
                    <code style="background: #0a0a0a; padding: 2px 6px; border-radius: 3px;">python3 -m control.security_review export</code> - Export CSV
                </div>
            </div>`;
            
            content.innerHTML = html;
        }
        
        // Update security badge
        function updateSecurityBadge() {
            fetch('/security/stats')
                .then(r => r.json())
                .then(stats => {
                    const badge = document.getElementById('securityBadge');
                    if (stats.pending > 0) {
                        badge.textContent = stats.pending;
                        badge.style.display = 'inline-block';
                    } else {
                        badge.style.display = 'none';
                    }
                })
                .catch(err => console.error('Error updating badge:', err));
        }
        
        // Initialize security features
        updateSecurityBadge();
        setInterval(updateSecurityBadge, 30000); // Update every 30s
    </script>'''
    
    script_end = "    </script>"
    # Find the last occurrence of </script>
    last_script_idx = content.rfind(script_end)
    if last_script_idx != -1:
        content = content[:last_script_idx] + security_js + content[last_script_idx:]
        print("  ✓ Added security JavaScript")
    else:
        print("  ✗ Could not find </script> tag")
    
    # Write patched file
    with open(output_file, 'w') as f:
        f.write(content)
    
    print(f"\n✅ Security UI patching complete!")
    print(f"📄 Output file: {output_file}")
    return True

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 apply_security_ui.py <input_file> [output_file]")
        print("\nExample:")
        print("  python3 apply_security_ui.py web_agent_v2.1.py web_agent_v2.1_with_security_ui.py")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else input_file.replace('.py', '_with_security_ui.py')
    
    if not os.path.exists(input_file):
        print(f"❌ Error: Input file not found: {input_file}")
        sys.exit(1)
    
    print(f"📥 Input:  {input_file}")
    print(f"📤 Output: {output_file}")
    print()
    
    if patch_web_agent(input_file, output_file):
        print("\n🎉 DONE! Now:")
        print(f"  1. Stop AVA: lsof -ti:5002 | xargs kill -9")
        print(f"  2. Backup:  cp web_agent_v2.1.py web_agent_v2.1.py.backup")
        print(f"  3. Replace: cp {output_file} web_agent_v2.1.py")
        print(f"  4. Start:   python3 web_agent_v2.1.py")
        print(f"  5. Access:  http://172.24.212.81:5002")
        print(f"  6. Click 🛡️ Security button in sidebar!")
    else:
        print("\n❌ Patching failed - check errors above")
        sys.exit(1)

if __name__ == '__main__':
    main()
