# ─────────────────────────────────────────────────────────────────────────────
# AVA OPA Policy: cve_scanner
# Package: ava.tools.cve_scanner
# Risk Tier: 1 (Read-Only)
# ─────────────────────────────────────────────────────────────────────────────

package ava.tools.cve_scanner

import future.keywords.if
import future.keywords.in

# ─────────────────────
# Default: deny all
# ─────────────────────
default allow := false

# ─────────────────────
# Allow if all checks pass
# ─────────────────────
allow if {
    not deny_remote_host
    not deny_invalid_severity
    not deny_package_filter_too_large
}

# ─────────────────────
# Deny Rules
# ─────────────────────

# v1: only localhost is supported
deny_remote_host if {
    input.parameters.host != "localhost"
    input.parameters.host != ""
    input.parameters.host != null
}

# Severity must be a valid value
valid_severities := {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

deny_invalid_severity if {
    input.parameters.severity_filter != null
    not input.parameters.severity_filter in valid_severities
}

# Package filter sanity: no more than 50 specific packages at once
deny_package_filter_too_large if {
    count(input.parameters.package_filter) > 50
}

# ─────────────────────
# Audit metadata
# ─────────────────────
audit := {
    "tool": "cve_scanner",
    "risk_tier": 1,
    "read_only": true,
    "host": input.parameters.host,
    "severity_filter": input.parameters.severity_filter,
    "package_count": count(input.parameters.package_filter) if input.parameters.package_filter != null else 0,
    "timestamp": input.request_time
}

# ─────────────────────
# Violation messages
# ─────────────────────
violations[msg] if {
    deny_remote_host
    msg := sprintf(
        "cve_scanner v1 only supports host='localhost'. Got: '%v'. Remote scanning requires Tool B (patch_executor with SSH).",
        [input.parameters.host]
    )
}

violations[msg] if {
    deny_invalid_severity
    msg := sprintf(
        "Invalid severity_filter '%v'. Must be one of: LOW, MEDIUM, HIGH, CRITICAL.",
        [input.parameters.severity_filter]
    )
}

violations[msg] if {
    deny_package_filter_too_large
    msg := sprintf(
        "package_filter has %v entries. Maximum is 50 per request.",
        [count(input.parameters.package_filter)]
    )
}
