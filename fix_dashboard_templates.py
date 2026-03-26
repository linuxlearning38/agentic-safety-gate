#!/usr/bin/env python3
"""
Quick fix for Security Dashboard template literals
Removes spaces in ${} that prevent JavaScript interpolation
"""

import sys

def fix_templates(filename):
    print(f"🔧 Fixing template literals in {filename}...")
    
    with open(filename, 'r') as f:
        content = f.read()
    
    # Fix all the template literal issues
    fixes = [
        ('$ {stats.blocked}', '${stats.blocked}'),
        ('$ {stats.pending}', '${stats.pending}'),
        ('$ {stats.threats_detected}', '${stats.threats_detected}'),
    ]
    
    fixed_count = 0
    for old, new in fixes:
        if old in content:
            content = content.replace(old, new)
            fixed_count += 1
            print(f"  ✓ Fixed: {old} → {new}")
    
    # Write back
    with open(filename, 'w') as f:
        f.write(content)
    
    print(f"\n✅ Fixed {fixed_count} template literals!")
    return True

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 fix_dashboard_templates.py <file.py>")
        sys.exit(1)
    
    fix_templates(sys.argv[1])
