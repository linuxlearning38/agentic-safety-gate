# AgentGuard Reports - Quick Reference

## 📁 Reports Location
**All exports go to:** `I:\ai-lab\reports\`
**WSL path:** `/mnt/i/ai-lab/reports/`

## 🚀 Quick Commands

### Export Last 7 Days (Default)
```bash
cd /mnt/i/ai-lab/projects/devops-agent/
python3 -m control.security_review export /mnt/i/ai-lab/reports/audit_$(date +%Y%m%d).csv 7
```

### Export Last 30 Days
```bash
python3 -m control.security_review export /mnt/i/ai-lab/reports/audit_monthly_$(date +%Y%m%d).csv 30
```

### Quick Weekly Export
```bash
python3 -m control.security_review export /mnt/i/ai-lab/reports/weekly_report.csv 7
```

### Copy from /tmp to Reports
```bash
cp /tmp/audit.csv /mnt/i/ai-lab/reports/agentguard_audit_$(date +%Y%m%d).csv
```

## 📊 Open in Excel
1. Open File Explorer
2. Navigate to `I:\ai-lab\reports\`
3. Double-click any `.csv` file
4. Opens directly in Excel!

## 🎯 Automated Weekly Report (Future Phase 2)
```bash
# Add to crontab for Sunday night exports
0 23 * * 0 cd /mnt/i/ai-lab/projects/devops-agent && python3 -m control.security_review export /mnt/i/ai-lab/reports/weekly_$(date +\%Y\%m\%d).csv 7
```

## 📋 Report Types

### Security Audit Report
- **File:** `audit_YYYYMMDD.csv`
- **Contains:** All commands, risk levels, decisions, threats
- **Usage:** Compliance, incident investigation

### Monthly Summary
- **File:** `audit_monthly_YYYYMMDD.csv`
- **Contains:** Last 30 days of activity
- **Usage:** Trend analysis, management reports

### Statistics JSON
```bash
python3 -m control.security_review stats 24 > /mnt/i/ai-lab/reports/stats_$(date +%Y%m%d).json
```

## 🎨 Excel Tips

### Recommended Pivot Tables:
1. **Commands by Risk Level**
   - Row: Risk Level
   - Values: Count of Commands

2. **Threats Over Time**
   - Row: Date
   - Values: Sum of Threats Count

3. **Decision Types**
   - Row: Decision
   - Values: Count

### Recommended Charts:
- Risk level distribution (Pie chart)
- Commands over time (Line chart)
- Blocked vs Executed (Bar chart)
