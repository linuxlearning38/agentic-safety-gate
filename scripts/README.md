# AVA Tool #14: cve_scanner

**Risk Tier:** 1 (Read-Only)  
**Requires confirmation:** No  
**Modifies system:** Never

---

## What It Does

Scans a Ubuntu host for known CVEs affecting its installed packages by:

1. Enumerating all installed packages via `dpkg-query`
2. Detecting the Ubuntu release codename from `/etc/os-release`
3. Querying the **Ubuntu Security API** per package for known CVEs
4. Filtering by release (e.g. only `jammy`-affecting CVEs)
5. Cross-checking with `apt-get --simulate upgrade` to flag which are **patchable right now**
6. Returning findings sorted by CVSS severity

---

## File Layout

```
tools/
└── cve_scanner/
    ├── cve_scanner.py        ← Main tool (AVA Tool #14)
    ├── cve_scanner.rego      ← OPA policy
    └── test_cve_scanner.py   ← pytest suite (unit + integration)
```

---

## Tool Registry Entry

Add to your `tools/registry.py`:

```python
from tools.cve_scanner.cve_scanner import execute as cve_scanner_execute, TOOL_DEFINITION as CVE_TOOL_DEF

TOOL_REGISTRY["cve_scanner"] = {
    **CVE_TOOL_DEF,
    "execute": cve_scanner_execute,
}
```

---

## Intent Router Patch

Add to `INTENT_PATTERNS` in `intent_router.py`:

```python
"cve_scan": {
    "tool": "cve_scanner",
    "keywords": [
        "cve", "vulnerability", "vulnerabilities", "vuln",
        "security scan", "ubuntu cve", "affected packages",
        "security advisory", "usn", "cvss", "security audit",
    ],
}
```

---

## Example Queries AVA Will Route Here

```
"scan for CVEs on this server"
"what vulnerabilities does this Ubuntu host have"
"check openssl CVEs"
"show me CRITICAL severity CVEs"
"are there any critical vulnerabilities in curl"
"run a security scan"
"check CVE for curl and bash"
```

---

## Sample Output

```json
{
  "scan_time": "2024-06-03T10:22:00+00:00",
  "host": "localhost",
  "ubuntu_release": "jammy",
  "packages_scanned": 5,
  "vulnerabilities_found": 2,
  "critical": 1,
  "high": 1,
  "medium": 0,
  "low": 0,
  "findings": [
    {
      "cve_id": "CVE-2024-12345",
      "package": "curl",
      "installed_version": "7.81.0-1ubuntu1.15",
      "fixed_version": "7.81.0-1ubuntu1.16",
      "severity": "CRITICAL",
      "cvss_score": 9.8,
      "description": "Buffer overflow in curl...",
      "usn_id": "USN-6000-1",
      "published": "2024-01-15T00:00:00",
      "source": "ubuntu",
      "patchable_now": true
    }
  ],
  "errors": []
}
```

---

## Rate Limits

| Source | Limit | Key Required |
|---|---|---|
| Ubuntu Security API | Generous, no stated limit | No |
| NVD (enrichment) | 5 req/30s | No (optional: 50 req/30s with key) |

Set `NVD_API_KEY` env var for NVD enrichment in production.

---

## Running Tests

```bash
# Unit tests only (no network)
pytest tests/test_cve_scanner.py -v

# With live API call (hits real Ubuntu Security API)
LIVE_TEST=1 pytest tests/test_cve_scanner.py -v -k test_live_scan_curl
```

---

## v1 Limitations

| Limitation | Reason |
|---|---|
| `localhost` only | Remote host scanning requires SSH executor (Tool B — planned) |
| Ubuntu only | Uses Ubuntu Security API + dpkg. RHEL/Debian need different sources |
| No auto-remediation | By design. Patch application is Tool B with Tier 3 risk + OPA gate |
| NVD enrichment optional | Separate rate limit, not called by default |

---

## Upgrade Path (v2)

- Add SSH executor for remote Ubuntu hosts
- Add NVD enrichment pass for all HIGH/CRITICAL findings  
- Integrate with AVA's ChromaDB knowledge base to explain CVEs in DevSecOps context
- Feed findings into `patch_executor` (Tool B) — Tier 3, human-gated
